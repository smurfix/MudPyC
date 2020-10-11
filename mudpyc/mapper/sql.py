from mudpyc.util import attrdict, combine_dict
from contextlib import contextmanager
from weakref import ref
from heapq import heappush,heappop
import simplejson as json

from sqlalchemy import ForeignKey, Column, Integer, MetaData, Table, String, Float, Text, create_engine, select, func, Binary, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, object_session, validates, backref
from sqlalchemy.schema import Index

from typing import Dict

from .const import SignalThis, SkipRoute, SkipSignal
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED
from ..driver import LocalDir

import trio
from contextvars import ContextVar

class NoData(RuntimeError):
    def __str__(self):
        if self.args:
            try:
                return "‹NoData:{jsa}›".format(jsa=':'.join(str(x) for x in self.args))
            except Exception:
                pass
        return "‹NoData›"
    pass

in_updater: ContextVar[bool] = ContextVar('in_updater', default=False)

@contextmanager
def SQL(cfg):
    url = cfg['sql']['url']
    sqlite = url.startswith("sqlite:")
    _idx={}
    if not sqlite:
        _idx['index'] = True

    engine = create_engine(
            url,
            #strategy=TRIO_STRATEGY
    )
    convention = {
        "ix": 'ix_%(column_0_label)s',
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s"
    }
    Base = declarative_base(metadata=MetaData(naming_convention=convention))

    room_cache = {}
    _cache_todo = set()  # rooms to be processed
    _cache_evt = trio.Event()  # set when there are rooms to be processed

    class _RoomCommon:
        """
        Abstract base class for both rooms and room caches
        """
        id_old:int = None
        id_mudlet:int = None
        label:str = None

        @property
        async def reachable(self):
            """
            This iterator yields room cache entries (actually, paths to
            them) reachable from this one. The room itself is included.

            This returns cache entries. Used when you only require the
            room IDs or the room's label.

            You must send a `mudpyc.mapper.const._PathSignal` instance back
            to the iterator, to tell it what to do next.
            """
            res = [(0,[self])]
            seen = set()
            c = None
            while res:
                d,h = heappop(res)
                r = h[-1]
                if r.id_old in seen:
                    continue
                seen.add(r.id_old)

                for rr,cc in r.exit_costs:
                    heappush(res,(d+cc, h+[rr]))

                c = (yield h)
                if c.done:
                    return
                if c.skip:
                    continue

        @property
        def cache(self):
            """
            Cache entry for this room
            """
            ...

        @property
        def exit_costs(self):
            """
            (next_room,cost) iterator
            """
            ...

    class CachedRoom(_RoomCommon):
        id_old = None
        id_mudlet = None
        label = None

        def __new__(cls, room):
            if not room.id_mudlet:
                return None
            try:
                res = room_cache[room.id_mudlet]
            except KeyError:
                res = object.__new__(cls)
            else:
                res._reset(room)
            return res

        def __init__(self, room):
            if self.id_old is not None:
                return
            self.id_mudlet = room.id_mudlet
            self.id_old = room.id_old
            self.reset()
            room_cache[self.id_old] = self

        @property
        def cache(self):
            return self

        def reset(self):
            dr = r_mudlet(self.id_mudlet)
            self._reset(dr)

        def _reset(self, room):
            self._exits = {}
            self.label = room.label
            for x in room.exits:
                if x.dst is None or not x.dst.id_mudlet:
                    continue
                self._exits[x.dst.id_mudlet] = x.cost

        @property
        def exit_costs(self):
            """
            cache>cost iterator
            """
            for d,c in self._exits.items():
                if d in room_cache:
                    dc = room_cache[d]
                else:
                    try:
                        r = r_old(d)
                    except KeyError:
                        continue
                    dc = CachedRoom(r)
                yield dc,c


    class _AddOn:
        @property
        def _s(self):
            return object_session(self)
        @property
        def _m(self):
            return self._s._mud__main()

    assoc_skip_room = Table('assoc_skip_room', Base.metadata,
        Column('skip_id', Integer, ForeignKey('skip.id')),
        Column('room_id', Integer, ForeignKey('rooms.id_old'), **_idx),
        *(() if sqlite else (Index("assoc_skip_idx","skip_id","room_id",unique=True),)),
        
    )

    assoc_seen_room = Table('seen_in', Base.metadata,
        Column('seen_id', Integer, ForeignKey('seen.id')),
        Column('room_id', Integer, ForeignKey('rooms.id_old'), **_idx),
        *(() if sqlite else (Index("assoc_seen_idx","seen_id","room_id",unique=True),)),
    )

    class Area(_AddOn, Base):
        F_IGNORE = (1<<0)

        __tablename__ = "area"
        id = Column(Integer, primary_key=True)
        name = Column(String(100))
        flag = Column(Integer, nullable=False, default=0)

        rooms = relationship("Room", back_populates="area")

    class Config(_AddOn, Base):
        __tablename__ = "cfg"
        id = Column(Integer, primary_key=True)
        name = Column(String(20), unique=True)
        _value = Column(Binary)

        @property
        def value(self):
            return json.loads(self._value)
        
        @value.setter
        def value(self, value):
            self._value = json.dumps(value)

    class Keymap(_AddOn, Base):
        __tablename__ = "keys"
        id = Column(Integer, primary_key=True)
        mod = Column(Integer)
        val = Column(Integer)
        text = Column(String(255))
        Index("keys_idx","mod","val",unique=True)

    class Exit(_AddOn, Base):
        __tablename__ = "exits"
        id = Column(Integer, primary_key=True)

        src_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="CASCADE"), nullable=False, **_idx)
        dir = Column(String(50), nullable=False)
        dst_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="SET NULL"), nullable=True, **_idx)

        det = Column(Integer, nullable=False, default=0) # ways with multiple destinations ## TODO
        cost = Column(Integer, nullable=False, default=1)
        steps = Column(String(255), nullable=True)
        flag = Column(Integer, nullable=False, default=0)
        delay = Column(Integer, nullable=False, default=0)
        feature_id = Column(Integer, ForeignKey("feature.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)

        src = relationship("Room", back_populates="exits", foreign_keys="Exit.src_id")
        dst = relationship("Room", back_populates="r_exits", foreign_keys="Exit.dst_id")
        feature = relationship("Feature", backref="exits")

        F_IN_MUDLET = (1<<0)

        @validates("dir")
        def dir_min_len(self, key, dir) -> str:
            if not dir:
                raise ValueError(f'dir {dir!r} empty')
            # TODO we might want to protect against "n" if it should really be "north"
            return dir

        @property
        def flag_str(self):
            res = ""
            ## if self.flag & Exit.F_IN_MUDLET:
            ##     res += "m"
            return res

        @property
        def info_str(self):
            if self.dst_id:
                res = _("Exit: {self.src.id_str} via {self.dir} to {self.dst.id_str}").format(self=self)
            else:
                res = _("Exit: {self.src.id_str} via {self.dir}").format(self=self)
            if self.cost != 1:
                res += _(" :{cost}").format(cost=self.cost)
            if self.steps:
                res += _(" ({lsm})").format(lsm=len(self.moves))
            if self.flag_str:
                res += " "+self.flag_str
            return f"‹{res}›"

        @property
        def moves(self):
            """
            List of moves to get there.
            """
            if self.steps is not None:
                # Yes this implies that if it's empty you just sit there
                return self.steps.split("\n")
            else:
                return [self.dir]

        @property
        def back_feature(self):
            if self.dst is None:
                return None
            try:
                x = self.dst.exit_to(self.src)
            except KeyError:
                return None
            return x.feature

    class Room(_RoomCommon, _AddOn, Base):
        __tablename__ = "rooms"
        id_old = Column(Integer, nullable=True, primary_key=True)
        id_mudlet = Column(Integer, nullable=True, unique=True)
        id_gmcp = Column(String(100), nullable=True, unique=True)
        name = Column(String(255), nullable=True) ## **_idx
        label = Column(String(50), nullable=True)
        # long_descr = Column(Text)
        pos_x = Column(Integer, nullable=False, default=0)
        pos_y = Column(Integer, nullable=False, default=0)
        pos_z = Column(Integer, nullable=False, default=0)
        last_visit = Column(Integer, nullable=True, unique=True)
        area_id = Column(Integer, ForeignKey("area.id", onupdate="CASCADE",ondelete="RESTRICT"), nullable=True, **_idx)
        label_x = Column(Float, nullable=False, default=0)
        label_y = Column(Float, nullable=False, default=0)
        flag = Column(Integer, nullable=False, default=0)

        # room title from MUD
        last_shortname = None

        # when changing an area
        orig_area = None
        info_area = None

        F_NO_EXIT = (1<<0)
        F_NO_GMCP_ID = (1<<1)
        F_MOD_SHORTNAME = (1<<2)
        F_NO_AUTO_EXIT = (1<<3)

        area = relationship(Area, back_populates="rooms")
        exits = relationship(Exit,
            primaryjoin=id_old == Exit.src_id, foreign_keys=[Exit.src_id],
            cascade="delete", passive_deletes=True)
        r_exits = relationship(Exit,
            primaryjoin=id_old == Exit.dst_id, foreign_keys=[Exit.dst_id])

        long_descr = relationship("LongDescr", uselist=False, cascade="delete")
        note = relationship("Note", uselist=False, cascade="delete")

        @validates("id_mudlet")
        def mudlet_ok(self, key, id) -> str:
            if id is not None and id < 0:
                raise ValueError('Cannot be negative')
            return id

        @validates("id_gmcp")
        def gmcp_min_len(self, key, id_gmcp) -> str:
            if id_gmcp is not None and len(id_gmcp) < 8:  # oben
                raise ValueError(f'GMCP id {id_gmcp!r} too short')
            return id_gmcp

        @property
        def cache(self):
            return room_cache[self.id_old]

        @property
        def exit_costs(self):
            """
            room>cost iterator

            Same semantics as RoomCache.exit_costs
            """
            for x in self.exits:
                if x.dst_id is None:
                    continue
                yield x.dst,x.cost

        @property
        def info_str(self):
            return _("‹{self.id_str} {self.exit_str} {self.name}›").format(self=self)

        @property
        def idn_str(self):
            n = self.name.rstrip(".")
            if len(n)>30:
                n = f"{n[:19]}…{n[-10:]}"
            n = n.replace(" ","_")
            return _("{self.id_str}:{n}").format(self=self,n=n)

        @property
        def idnn_str(self):
            n = self.name.rstrip(".")
            return _("‹{self.id_str}:{n}›").format(self=self,n=n)

        @property
        def id_str(self):
            if self.id_mudlet:
                return str(self.id_mudlet)
            else:
                return str(-self.id_old)

        def next_word(self):
            while True:
                res = session.query(WordRoom).filter(WordRoom.room_id==self.id_old, WordRoom.flag == 0).first()
                if res is not None:
                    wa = session.query(WordAlias).filter(WordAlias.name == res.word.name).one_or_none()
                    if wa is not None:
                        session.delete(res)
                        continue
                session.commit()
                return res

        def reset_words(self):
            for wr in session.query(WordRoom).filter(WordRoom.room_id==self.id_old, WordRoom.flag == 1).all():
                wr.flag = 0
            session.commit()


        def set_id_mudlet(self, id_mudlet):
            """
            Clear the "exit is set in Mudlet" flags because those stub
            exits to nonexisting rooms suddenly aren't stub exits any more
            so when we next look at them they need to be reconsidered
            """
            self.id_mudlet = id_mudlet
            for x in self.exits:
                x.flag &=~ Exit.F_IN_MUDLET;
            for x in self.r_exits:
                x.flag &=~ Exit.F_IN_MUDLET;

        def with_word(self, word, create=False):
            """
            Return the WordRoom entry for this word, if known.
            """
            if isinstance(word, str):
                word = get_word(word, create=create)
                if word is None:
                    return None
            res = session.query(WordRoom).filter(WordRoom.room_id==self.id_old,WordRoom.word_id==word.id).one_or_none()
            if res is None and create:
                res = WordRoom(word=word, room=self)
                session.add(res)
            return res

        @property
        def exit_str(self):
            if not self.exits:
                return ":"
            ex = []
            for x in self.exits:
                d = x.dir
                if d.startswith("betrete haus von "):
                    d = "b-h-"+d[17:]
                elif d.startswith("betrete "):
                    d = "b-"+d[8:]
                if x.dst_id:
                    if x.dst.id_mudlet is None:
                        d += f"=-{x.dst.id_old}"
                    else:
                        d += f"={x.dst.id_mudlet}"
                ex.append(d)
            return ":"+":".join(ex)+":"

        def visited(self):
            lv = session.query(func.max(Room.last_visit)).scalar() or 0
            if self.last_visit != lv:
                self.last_visit = lv+1

        @property
        def cost(self):
            """
            A room's cost is defined as the min cost of all its exits
            """
            return min(x.cost for x in self.exits)

        def set_cost(self, cost):
            """
            Adjust a room's cost by shifting by the diff to the exits' minimum

            Not transmitted to the MUD
            """
            w = cost - self.cost
            for x in self.exits:
                x.cost += w

        async def set_area(self, area, force=False):
            mud = self._m.mud

            if self.area == area and not force:
                return
            self.area = area
            if self.id_mudlet:
                await mud.setRoomArea(self.id_mudlet,area.name)

        async def set_name(self, name, force=False):
            mud = self._m.mud

            if self.name == name and not force:
                return
            self.name = name
            if self.id_mudlet:
                await self.mud.setRoomName(self.id_mudlet, name)

        @property
        def open_exits(self):
            """Does this room have open exits?
            0: no, 1: yes, 2: yes special exits, 3: yes unmapped
            """
            res = 0
            m = self._m
            for x in self.exits:
                if x.dst is None:
                    if m.dr.is_std_dir(x.dir):
                        res = max(res,1)
                    else:
                        res = max(res,2)
                elif x.dst.id_mudlet is None:
                    res = max(res,3)
            return res

        def exit_at(self,d, prefer_feature=False):
            """
            Return the exit(s) in a specific direction
            If prefer_feature is set, and another exits with a feature
            ID set goes to the same place, use that.
            """
            res = None
            for x in self.exits:
                if x.dir == d:
                    res = x
                    break
            else:
                raise KeyError()
            if not prefer_feature or not res.dst_id or res.feature_id:
                return res
            for x in self.exits:
                if x.dst_id == res.dst_id and x.feature_id:
                    return x
            return res

        def exit_to(self,d):
            """
            Return (one of) the exit(s) to a specific room

            If one of the exits has the Feature flag set, use that.
            """
            res = None
            if isinstance(d,Room):
                d = d.id_old
            for x in self.exits:
                if x.dst_id == d:
                    if x.feature_id:
                        return x
                    res = x
            if not res:
                raise KeyError()
            return res

        async def set_exit(self,d, v=True, *, force=True, skip_mud=False):
            """
            Add an exit d going to room v.
            v=True (default): add but don't overwrite if it points somewhere.
            v=False: delete the destination
            v=None: delete the exit 
            force: change even if rooms differ, defaults to True.

            Returns a tuple: (Exit, changedFlag)
            """
            # TODO split this up
            if not len(d):
                return 

            changed = False
            if v is True or v is False:
                for x in self.exits:
                    if x.dir == d:
                        if x.dst_id:
                            if v:
                                v = x.dst
                            else:
                                x.dst = None
                                changed = True
                        break
                else:
                    x = Exit(src=self, dir=d)
                    self._s.add(x)
                    changed = True
            else:
                for x in self.exits:
                    if x.dir == d:
                        if v:
                            if x.dst and not force:
                                pass
                            elif x.dst != v:
                                x.dst = v
                                changed = True
                        else:
                            self._s.delete(x)
                            changed = True
                        break
                else:
                    if v:
                        x = Exit(src=self, dir=d, dst=v)
                        self._s.add(x)
                        changed = True

            if (changed or not (x.flag & Exit.F_IN_MUDLET)) and not skip_mud:
                if x.flag is None:
                    x.flag = 0 ## DUH?
                if self.id_mudlet and await self.set_mud_exit(d,v):
                    x.flag |= Exit.F_IN_MUDLET
                else:
                    x.flag &=~ Exit.F_IN_MUDLET

            update_cache(self)
            self._s.commit()

            return x,changed

        async def del_exit(self,d, skip_mud=False):
            await self.set_exit(d,None, skip_mud=skip_mud)

        @property
        async def mud_exits(self) -> Dict[str, int]:
            """
            Reads room exits from Mudlet.

            Returns a direction > Mudlet-ID dict.
            """
            m = self._m
            mud = m.mud
            res = {}
            if not self.id_mudlet:
                raise NoData
            x = await mud.getRoomExits(self.id_mudlet)
            if x:
                x = { m.dr.intl2loc(d):r for d,r in x[0].items() }
            else:
                x = {}
            y = await mud.getSpecialExitsSwap(self.id_mudlet)
            if y:
                y = y[0]
            else:
                y = {}
            return combine_dict(x,y)

        async def set_mud_exit(self,d:LocalDir,v=True):
            m = self._m
            mud = self._m.mud
            if not self.id_mudlet:
                raise RuntimeError("no id_mudlet "+self.idnn_str)

            changed = False
            d = m.dr.loc2intl(d)
            if v and v is not True and v is not False and v.id_mudlet:  # set
                if m.dr.is_mudlet_dir(d):
                    await mud.setExit(self.id_mudlet,v.id_mudlet,d)
                else:
                    await mud.addSpecialExit(self.id_mudlet,v.id_mudlet,d)
                changed = True
            else:
                if not v:  # delete
                    if m.dr.is_mudlet_dir(d):
                        await mud.setExit(self.id_mudlet, -1, d)
                    else:
                        await mud.removeSpecialExit(self.id_mudlet,d)
                    changed = True
                if v is not None: # stub
                    x = (await mud.getRoomExits(self.id_mudlet))
                    x = x[0] if len(x) else []
                    if d not in x:
                        if m.dr.is_mudlet_dir(d):
                            await mud.setExitStub(self.id_mudlet, d, True)
                        else: # special exit
                            # a special exit without a destination doesn't work
                            pass # await mud.addSpecialExit(self.id_mudlet,0,d) # ?
                        changed = True

            await mud.setRoomEnv(self.id_mudlet, ENV_OK+self.open_exits)
            return changed


        def has_thing(self, txt):
            t = get_thing(txt)
            self.things.append(t)


        def __lt__(self, other):
            if isinstance(other,Room):
                other = other.id_old
                return self.id_old < other

    def update_cache(room):
        if in_updater.get():
            return
        if room.id_old not in _cache_todo:
            _cache_todo.add(room.id_old)
            _cache_evt.set()

    async def cache_updater():
        # standard decorator style
        nonlocal _cache_evt
        in_updater.set(True)

        @event.listens_for(Room, 'load')
        def receive_load(room, context):
            "listen for the 'load' event"
            if room.id_old not in room_cache:
                update_cache(room)

        evts = []
        while True:
            if not _cache_todo:
                await _cache_evt.wait()
                _cache_evt = trio.Event()

            while _cache_todo:
                await trio.sleep(0)
                r = _cache_todo.pop()

                try:
                    room = r_old(r)
                except NoData:
                    pass # deleted
                else:
                    CachedRoom(room)

    class LongDescr(_AddOn, Base):
        __tablename__ = "longdescr"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False, **_idx)
        descr = Column(Text, nullable=False)

#        room = relationship("Room", uselist=False,
#            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Thing(_AddOn, Base):
        __tablename__ = "seen"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(100), nullable=False)

        rooms = relationship("Room", secondary=assoc_seen_room, backref="things")

    class Feature(_AddOn, Base):
        F_BACKOUT = (1<<0)

        __tablename__ = "feature"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(50), nullable=False)
        flag = Column(Integer, nullable=False, default=0)

        enter = Column(Text, nullable=False, default="")
        exit = Column(Text, nullable=False, default="")

        @property
        def enter_moves(self):
            if self.enter:
                return self.enter.split("\n")
            else:
                return []

        @property
        def exit_moves(self):
            if self.exit:
                return self.exit.split("\n")
            else:
                return []


    class Note(_AddOn, Base):
        __tablename__ = "notes"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False, **_idx)
        note = Column(Text, nullable=False)

#        room = relationship("Room", uselist=False,
#            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Skiplist(_AddOn, Base):
        __tablename__ = "skip"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(50), nullable=False)
        rooms = relationship("Room", secondary=assoc_skip_room, backref="skiplists")

    class Word(_AddOn, Base):
        __tablename__ = "words"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(50), nullable=False, unique=True)
        flag = Column(Integer, nullable=False, default=0)
        # 0 std, 2 skip

        _alias_for = None

        def alias_for(self, w):
            """
            This word is an alias for another word.
            Replace it.
            """
            if w is None:
                import pdb;pdb.set_trace()
            if isinstance(w,str):
                w = get_word(w, create=True)
            if w is self:
                raise RuntimeError("Cannot replace with myself")
            if w._alias_for is not None:
                raise RuntimeError("already replaced")

            for wr in self.in_rooms:
                if wr.room.with_word(w) is None:
                    # Original not known: update entry
                    wr.word = w
                else:
                    session.delete(wr)
            wa = WordAlias(name=self.name, word=w)
            session.delete(self)
            session.add(wa)
            self._alias_for = w
            return w

    class WordAlias(_AddOn, Base):
        __tablename__ = "wordalias"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(50), nullable=False)
        word_id = Column(Integer, ForeignKey("words.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)

        word = relationship("Word", backref=backref("aliases", cascade="all, delete-orphan"))

    class WordRoom(_AddOn, Base):
        __tablename__ = "wordroom"
        id = Column(Integer, nullable=True, primary_key=True)

        word_id = Column(Integer, ForeignKey("words.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False, **_idx)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="CASCADE"), nullable=False, **_idx)

        flag = Column(Integer, nullable=False, default=0)
        # 0 notscanned, 1 scanned, 2 skipped, 3 marked important?

        word = relationship("Word", backref=backref("in_rooms", cascade="all, delete-orphan"))
        room = relationship("Room", backref=backref("words", cascade="all, delete-orphan"))

        def alias_for(self, w):
            """
            This word is an alias for another word.
            Replace it.
            """
            ww = self.word.alias_for(w)
            return session.query(WordRoom).filter(Room.id_old == self.room.id_old, Word.id == ww.id).one_or_none()

    class Quest(_AddOn, Base):
        __tablename__ = "quest"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String(50), nullable=False)
        flag = Column(Integer, nullable=False, default=0)
        step = Column(Integer, nullable=True)

        def id_str(self):
            if self.step:
                return _("‹{self.name}›").format(self=self)
            else:
                return _("‹{self.name}:{self.step}›").format(self=self)

        def add_step(self, **kw):
            step = self.last_step_nr +1
            qs = QuestStep(quest_id=self.id, step=step, **kw)
            session.add(qs)
            return qs

        @property
        def last_step_nr(self):
            return session.query(func.max(QuestStep.step)).filter(QuestStep.quest==self).scalar() or 0
        def step_at(self, nr=None):
            if nr is None:
                nr = self.step
                if nr is None:
                    return None
            return session.query(QuestStep).filter(QuestStep.quest_id == self.id, QuestStep.step==nr).one_or_none()

        @property
        def current_step(self):
            return self.step_at(None)

    class QuestStep(_AddOn, Base):
        __tablename__ = "queststep"
        id = Column(Integer, nullable=True, primary_key=True)

        quest_id = Column(Integer, ForeignKey("quest.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False, **_idx)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="CASCADE"), nullable=False, **_idx)
        step = Column(Integer, nullable=False)
        # WARNING quest_id+step should be unique but cannot be because
        # renumbering then won't work because mysql doesn't support
        # deferring unique key checks until commit, and sqlalchemy doesn't
        # support order_by when updating
        command = Column(String(255), nullable=False)

        flag = Column(Integer, nullable=False, default=0)
        # 1 command, 2 stop execution

        quest = relationship("Quest", backref=backref("steps", cascade="all, delete-orphan"))
        room = relationship("Room", backref=backref("queststeps", cascade="all, delete-orphan"))

        def set_step_nr(self, step):
            """
            Move steps around
            """
            # at first we always move the current step to the end
            if step is not None and step == self.step:
                return
            nsteps = (session.query(func.max(Room.last_visit)).scalar() or 0)+1
            if step is None:
                step = nsteps
            elif step >= nsteps: # don't put beyond the end
                return
            ostep,self.step = self.step,0

            if ostep < step:
                # move stuff down
                session.query(QuestStep).filter(QuestStep.quest_id == self.quest_id, QuestStep.step > ostep, QuestStep.step <= step).update({QuestStep.step:QuestStep.step-1})
            else:
                # move stuff up
                session.query(QuestStep).filter(QuestStep.quest_id == self.quest_id, QuestStep.step >= step, QuestStep.step < ostep).update({QuestStep.step:QuestStep.step+1})
            self.step = step
            session.commit()

        def delete(self):
            step = self.step
            session.delete(self)
            session.query(QuestStep).filter(QuestStep.quest_id == self.quest_id, QuestStep.step > step).update({QuestStep.step:QuestStep.step-1})
            session.commit()


    def r_old(room):
        res = session.query(Room).filter(Room.id_old == room).one_or_none()
        if res is None:
            raise NoData("id_old",room)
        return res

    def r_mudlet(room):
        res = session.query(Room).filter(Room.id_mudlet == room).one_or_none()
        if res is None:
            raise NoData("id_mudlet",room)
        return res

    def r_hash(room):
        res = session.query(Room).filter(Room.id_gmcp == room).one_or_none()
        if res is None:
            raise NoData("id_hash",room)
        if res.flag & Room.F_NO_GMCP_ID:
            raise NoData("id_hash:no_gmcp",room)
        return res

    def r_new():
        res=Room()
        session.add(res)
        return res

    def get_skiplist(name, create=None):
        sk = session.query(Skiplist).filter(Skiplist.name == name).one_or_none()
        if sk is None:
            if create is False:
                raise NoData("name",name)
            sk = Skiplist(name=name)
            session.add(sk)
        return sk

    def get_word(name, create=False):
        w = session.query(Word).filter(Word.name == name).one_or_none()
        if w is None:
            w = session.query(WordAlias).filter(WordAlias.name == name).one_or_none()
            if w is not None:
                w = w.word
        if w is None and create:
            w = Word(name=name)
            session.add(w)
        if w is not None and w._alias_for:
            w = w._alias_for
        return w

    def get_thing(name):
        th = session.query(Thing).filter(Thing.name == name).one_or_none()
        if th is None:
            th = Thing(name=name)
            session.add(th)
            session.commit()
        return th

    def get_quest(name_or_id):
        if isinstance(name_or_id, int):
            qq = Quest.id == name_or_id
        else:
            qq = Quest.name == name_or_id
        q = session.query(Quest).filter(qq).one_or_none()
        if q is None:
            raise KeyError(name_or_id)
        return q

    def get_feature(name):
        f = session.query(Feature).filter(Feature.name == name).one_or_none()
        if f is None:
            raise KeyError(name)
        return f

    class Cfg:
        def __init__(self):
            self._cache = {}
        def __getitem__(self, name):
            if name in self._cache:
                return self._cache[name]
            f = session.query(Config).filter(Config.name == name).one_or_none()
            if f is None:
                raise KeyError(name)
            value = f.value
            self._cache[name] = value
            return value
        
        def __setitem__(self, name, value):
            f = session.query(Config).filter(Config.name == name).one_or_none()
            if f is None:
                f = Config(name=name)
                f.value = value
                session.add(f)
            session.commit()
            self._cache[name] = value

    def setup(server):
        session._mud__main = ref(server)

    Session=sessionmaker(bind=engine)
    #conn = await engine.connect()
    session=Session()
    res = attrdict(db=session, q=session.query,
            setup=setup, cache_updater=cache_updater,
            Room=Room, Area=Area, Exit=Exit, Skiplist=Skiplist, Quest=Quest,
            Thing=Thing, Feature=Feature, LongDescr=LongDescr, Note=Note,
            Keymap=Keymap,
            r_hash=r_hash, r_old=r_old, r_mudlet=r_mudlet, r_new=r_new,
            skiplist=get_skiplist, word=get_word, thing=get_thing,
            quest=get_quest, feature=get_feature,
            cfg=Cfg(),
            commit=session.commit, rollback=session.rollback,
            add=session.add, delete=session.delete,
            WF_SCANNED=1, WF_SKIP=2, WF_IMPORTANT=3,
            )
    session._main = ref(res)
    try:
        yield res
    finally:
        session.close()

