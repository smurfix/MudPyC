-- This module connects Mudlet to the "mudlet" Python module,
-- allowing you to access all of Mudlet from Python instead of Lua.
-- 
-- Lag per request is around a millisecond or so. You can do things in parallel.
-- 

py = py or {}
py.action = py.action or {}

function py.init(port)
    assert(py.handler_id_done == nil, "Already running")
    py.handler = py.handler or {}
    py.callbacks = py.callbacks or {}
    py.post_open = 0
    py.backoff = 0
    py.seq = 0
    py.connected = false
    
    py.url = "http://127.0.0.1:" .. port .. "/json"
    
    py.handler_id_done = registerAnonymousEventHandler("sysPostHttpDone", py.postDone)
    py.handler_id_err = registerAnonymousEventHandler("sysPostHttpError", py.postError)
    py.handler_id_gdone = registerAnonymousEventHandler("sysGetHttpDone", py.postDone)
    py.handler_id_gerr = registerAnonymousEventHandler("sysGetHttpError", py.postError)
    py.handler_id_pdone = registerAnonymousEventHandler("sysPutHttpDone", py.postDone)
    py.handler_id_perr = registerAnonymousEventHandler("sysPutHttpError", py.postError)
    py.post({action="init"})
end

function py.exit()
    if py.handler_id_done then
        killAnonymousEventHandler(py.handler_id_done)
        killAnonymousEventHandler(py.handler_id_err)
        killAnonymousEventHandler(py.handler_id_gdone)
        killAnonymousEventHandler(py.handler_id_gerr)
        killAnonymousEventHandler(py.handler_id_pdone)
        killAnonymousEventHandler(py.handler_id_perr)
        py.handler_id_done = nil
        py.handler_id_err = nil
        py.handler_id_gdone = nil
        py.handler_id_gerr = nil
        py.handler_id_pdone = nil
        py.handler_id_perr = nil
    end
    py._clear_callbacks()
    if py.timer then
        killTimer(py.timer)
        py.timer = nil
    end
    if py.file then
        py.file:close()
        py.file = nil
    end
    if py.connected then
        raiseEvent("PyDisconnect", py.url)
        py.connected = false
    end
    if py.handler then
        for _,hdl in pairs(py.handler) do
            killAnonymousEventHandler(hdl)
        end
        py.handler = {}
    end
    py._clear_callbacks()
end

function py._clear_callbacks()
    local cbs = py.callbacks
    py.callbacks = {}
    for _,cb in pairs(py.callbacks) do
        cb(false, "Disconnected")
    end
end

function py.post(msg)
    msg = yajl.to_string(msg) .. "\n"
    if py.file then
        print("writing" ..msg)
        py.file:write(string.len(msg) .. "\n" .. msg)
        py.file:flush()
    else
        py._post(msg)
    end
end

function py._post(msg)
    py.post_open = py.post_open + 1
    local ok,url
    if msg == nil and getHTTP ~= nil then  -- mod by Smurf
        ok,url = getHTTP(py.url)
    else
        if msg == nil then
            if py.post_open > 1 then
                py.post_open = py.post_open - 1
                return
            end
            msg = yajl.to_string({action="poll"}).."\n"
        end
        if py.post_open > 1 then
            ok,url = putHTTP(msg, py.url, {["Content-Type"]="application/json"})
        else
            ok,url = postHTTP(msg, py.url, {["Content-Type"]="application/json"})
        end
    end
    if ok then
        if url ~= "" then
            py.url = url
        end
    else
        py.postError(nil, "POST failed", py.url)
    end
end

function py.postDone(_, url, msg)
    if url ~= py.url then print("PCMP",url) return end
    py.post_open = py.post_open - 1
    msg = yajl.to_value(msg)
    for _,m in ipairs(msg) do
        py.process(m)
    end
    py._poll()
end

function py._poll()
    if py.post_open > 0 then return end
    py._post()
end

function py.process(msg)
    if msg.action then
        local res = {pcall(py.action[msg.action], msg)}
        local r = res[1]
        table.remove(res, 1)
        if r then
            if not msg.seq then return end
            res = {result=res}
        else
            res = {error=res[1]}
        end
        res.seq = msg.seq -- no-op if missing
        py.post(res)
    else
        print("PY: No idea how to handle:",msg)
    end
end

function py.call(name, args, callback)
    assert(py.connected, "not connected")
    seq = py.seq+1
    py.seq = seq
    py.callbacks[seq] = callback
    py.post({action="call", call=name, data=args, cseq=seq})
end

function py.postError(_, msg, url)
    if url ~= py.url then return end
    py.post_open = py.post_open - 1
    if py.connected then
        py.connected = false
        raiseEvent("PyDisconnect", py.url)
    end
    if py.file ~= nil then  -- most likely dead
        py.file:close()
        py.file = nil
    end
    py._clear_callbacks()
    if py.timer == nil then
        py.backoff = py.backoff * 2 + 0.1
        if py.backoff > 5 then py.backoff = 5 end
        py.timer = tempTimer(py.backoff, py.postRetry)
    end
end

function py.postRetry()
    py.timer = nil
    py.post({action="init"})
end

function py.action.init(msg)
    if py.file then
        py.file:close()
        py.file = nil
    end
    if msg.fifo then
        py.file = io.open(msg.fifo, "w")
    end
    -- otherwise use HTTP
    py.connected = true
    py.post({action="up"})
    raiseEvent("PyConnect", py.url)
    py._poll()
end

function py.action.ping(msg)
    return "Pong"
end

function py.action.handle(msg)
    local evt = msg.event
    if py.handler[evt] then return false end
    local function hdl(...)
        py.post({event=evt, args={...}})
    end
    py.handler[hdl] = registerAnonymousEventHandler(evt, hdl)
    return true
end

function py.action.unhandle(msg)
    local evt = msg.event
    if not py.handler[evt] then return false end
    killAnonymousEventHandler(py.handler[hdl])
    py.handler[hdl] = nil
    return true
end

function py.action.call(msg)
    local args = msg.args or {}
    local self,func = nil,_G
    assert(#name > (msg.meth and 1 or 0), "no empty name!")
    for _,name in ipairs(msg.name) do
        self = func
        func = func[name]
    end
    if msg.dest then
        assert(#msg.dest > 0, "no empty name!")
        local res
        if msg.meth then
            res = func(self, unpack(args))
        else
            res = func(unpack(args))
        end
        local self,old = nil,_G
        for _,name in ipairs(msg.dest) do
            self = old
            old = old[name]
        end
        self[name] = msg.value
        return
   
    elseif msg.meth then
        return func(self, unpack(args))
    else
        return func(unpack(args))
    end
end

function py.action.get(msg)
    assert(#msg.name > 0, "no empty name!")
    val = _G
    for _,name in ipairs(msg.name) do
        val = val[name]
    end
    return val
end

function py.action.set(msg)
    local self,old = nil,_G
    assert(#msg.name > 0, "no empty name!")
    for _,name in ipairs(msg.name) do
        self = old
        old = old[name]
    end
    self[name] = msg.value
    if msg.old then return old end  -- otherwise return nothing
end

function py.action.event(msg)
    raiseEvent(msg.name, unpack(msg.args))
end

function py.action.result(msg)
    local seq = msg.cseq
    local cb = py.callbacks[seq]
    if cb then
        py.callbacks[seq] = nil
        if msg.error then
            cb(false, msg.error)
        else
            cb(true, unpack(msg.result))
        end
    end
end
