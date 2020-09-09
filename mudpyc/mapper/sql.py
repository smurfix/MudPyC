from mudpyc.util import attrdict, combine_dict
from contextlib import contextmanager
from weakref import ref
from heapq import heappush,heappop

from sqlalchemy import ForeignKey, Column, Integer, MetaData, Table, String, Float, Text, create_engine, select, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, object_session, validates, backref
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable, DropTable

from .const import SignalThis, SkipRoute, SkipSignal
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED

class NoData(RuntimeError):
    def __str__(self):
        if self.args:
            try:
                return "‹NoData:{jsa}›".format(jsa=':'.join(str(x) for x in self.args))
            except Exception:
                pass
        return "‹NoData›"
    pass

@contextmanager
def SQL(cfg):
    engine = create_engine(
            cfg.sql.url,
            #strategy=TRIO_STRATEGY
    )
    Base = declarative_base()
    class _AddOn:
        @property
        def _s(self):
            return object_session(self)
        @property
        def _m(self):
            return self._s._mud__main()

    assoc_skip_room = Table('assoc_skip_room', Base.metadata,
        Column('skip_id', Integer, ForeignKey('skip.id')),
        Column('room_id', Integer, ForeignKey('rooms.id_old'))
    )

    assoc_seen_room = Table('seen_in', Base.metadata,
        Column('seen_id', Integer, ForeignKey('seen.id')),
        Column('room_id', Integer, ForeignKey('rooms.id_old'))
    )

    class Area(_AddOn, Base):
        F_IGNORE = (1<<0)

        __tablename__ = "area"
        id = Column(Integer, primary_key=True)
        name = Column(String)
        flag = Column(Integer, nullable=False, default=0)

        rooms = relationship("Room", back_populates="area")

    class Exit(_AddOn, Base):
        __tablename__ = "exits"
        id = Column(Integer, primary_key=True)
        src_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="CASCADE"), nullable=False)
        dir = Column(String, nullable=False)
        dst_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
        det = Column(Integer, nullable=False, default=0) # ways with multiple destinations ## TODO
        cost = Column(Integer, nullable=False, default=1)
        steps = Column(String, nullable=True)
        flag = Column(Integer, nullable=False, default=0)
        delay = Column(Integer, nullable=False, default=0)
        feature_id = Column(Integer, ForeignKey("feature.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)

        src = relationship("Room", back_populates="_exits", foreign_keys="Exit.src_id")
        dst = relationship("Room", foreign_keys="Exit.dst_id")
        feature = relationship("Feature", backref="exits")

        F_IN_MUDLET = (1<<0)

        @validates("dir")
        def dir_min_len(self, key, dir) -> str:
            if dir is not None and len(dir) < 3:  # oben
                raise ValueError(f'dir {dir!r} too short')
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

    class Room(_AddOn, Base):
        __tablename__ = "rooms"
        id_old = Column(Integer, nullable=True, primary_key=True)
        id_mudlet = Column(Integer, nullable=True)
        id_gmcp = Column(String, nullable=True, unique=True)
        name = Column(String, nullable=True, index=True)
        label = Column(String)
        # long_descr = Column(Text)
        pos_x = Column(Integer)
        pos_y = Column(Integer)
        pos_z = Column(Integer)
        last_visit = Column(Integer, nullable=True, unique=True)
        area_id = Column(Integer, ForeignKey("area.id", onupdate="CASCADE",ondelete="RESTRICT"), nullable=True)
        label_x = Column(Float, default=0)
        label_y = Column(Float, default=0)
        flag = Column(Integer, nullable=False, default=0)

        # room title from MUD
        last_shortname = None

        # when changing an area
        orig_area = None
        info_area = None

        F_NO_EXIT = (1<<0)
        F_NO_GMCP_ID = (1<<1)
        F_MOD_SHORTNAME = (2<<1)

        area = relationship(Area, back_populates="rooms")
        _exits = relationship(Exit,
            primaryjoin=id_old == Exit.src_id, foreign_keys=[Exit.src_id],
            cascade="delete", passive_deletes=True)
        _r_exits = relationship(Exit,
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
            for x in self._exits:
                x.flag &=~ Exit.F_IN_MUDLET;
            for x in self._r_exits:
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
            if not self._exits:
                return ":"
            ex = []
            for x in self._exits:
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

        @property
        def exits(self):
            res = {}
            for x in self._exits:
                res[x.dir] = x.dst
            return res

        def visited(self):
            lv = session.query(func.max(Room.last_visit)).scalar() or 0
            if self.last_visit != lv:
                self.last_visit = lv+1

        @property
        def cost(self):
            """
            A room's cost is defined as the min cost of all its exits
            """
            return min(x.cost for x in self._exits)

        def set_cost(self, cost):
            """
            Adjust a room's cost by shifting by the diff to the exits' minimum

            Not transmitted to the MUD
            """
            w = cost - self.cost
            for x in self._exits:
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
            for x in self._exits:
                if x.dst is None:
                    if x.dir in m.loc_names:
                        res = max(res,1)
                    else:
                        res = max(res,2)
                elif x.dst.id_mudlet is None:
                    res = max(res,3)
            return res

        @property
        async def reachable(self):
            """
            This iterator yields rooms (actually, paths to rooms)
            reachable from this one. The room itself is included.

            TODO use a cache to speed up all of this.
            """
            res = [(0,[self])]
            seen=set()
            c=None
            while res:
                d,h = heappop(res)
                r = h[-1]
                if r.id_old in seen:
                    continue
                c = (yield h)
                seen.add(r.id_old)
                if c.done:
                    import pdb;pdb.set_trace()
                    return
                if c.skip:
                    continue
                for x in session.query(Exit).filter(Exit.src==r):
                    if x.dst_id is None:
                        continue
                    heappush(res,(d+x.cost, h+[x.dst]))

        def exit_at(self,d, prefer_feature=False):
            """
            Return the exit(s) in a specific direction
            If prefer_feature is set, and another exits with a feature
            ID set goes to the same place, use that.
            """
            res = None
            for x in self._exits:
                if x.dir == d:
                    res = x
                    break
            else:
                raise KeyError()
            if not prefer_feature or not res.dst_id or res.feature_id:
                return res
            for x in self._exits:
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
            for x in self._exits:
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
            x = self.exits
            changed = False
            if v is True or v is False:
                for x in self._exits:
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
                for x in self._exits:
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
                if await self.set_mud_exit(d,v):
                    x.flag |= Exit.F_IN_MUDLET
                else:
                    x.flag &=~ Exit.F_IN_MUDLET
            #self._exits = ":".join(f"{k}={v}" if v else f"{k}" for k,v in x.items())
            self._s.commit()

            return x,changed

        async def del_exit(self,d, skip_mud=False):
            await self.set_exit(d,None, skip_mud=skip_mud)

        @property
        async def mud_exits(self):
            m = self._m
            mud = m.mud
            res = {}
            if not self.id_mudlet:
                raise NoData
            x = await mud.getRoomExits(self.id_mudlet)
            if x:
                x = x[0]
            else:
                x = {}
            y = await mud.getSpecialExitsSwap(self.id_mudlet)
            if y:
                y = y[0]
            else:
                y = {}
            return combine_dict(m.itl2loc(x),y)

        async def set_mud_exit(self,d,v=True):
            m = self._m
            mud = self._m.mud
            if not self.id_mudlet:
                raise RuntimeError("no id_mudlet "+self.idnn_str)

            changed = False
            d = m.loc2itl(d)
            if v and v is not True and v is not False and v.id_mudlet:  # set
                if d in m.itl_names:
                    await mud.setExit(self.id_mudlet,v.id_mudlet,d)
                else:
                    await mud.addSpecialExit(self.id_mudlet,v.id_mudlet,d)
                changed = True
            else:
                if not v:  # delete
                    if d in m.itl_names:
                        await mud.setExit(self.id_mudlet, -1, d)
                    else:
                        await mud.removeSpecialExit(self.id_mudlet,d)
                    changed = True
                if v is not None: # stub
                    x = (await mud.getRoomExits(self.id_mudlet))
                    x = x[0] if len(x) else []
                    if d not in x:
                        if d in m.itl_names:
                            await mud.setExitStub(self.id_mudlet, d, True)
                        else: # special exit
                            await mud.addSpecialExit(self.id_mudlet,0,d) # ?
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

    class LongDescr(_AddOn, Base):
        __tablename__ = "longdescr"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False)
        descr = Column(Text, nullable=False)

#        room = relationship("Room", uselist=False,
#            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Thing(_AddOn, Base):
        __tablename__ = "seen"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String, nullable=False)

        rooms = relationship("Room", secondary=assoc_seen_room, backref="things")

    class Feature(_AddOn, Base):
        F_BACKOUT = (1<<0)

        __tablename__ = "feature"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String, nullable=False)
        flag = Column(Integer, nullable=False, default=0)

        enter = Column(Text, nullable=False, default="")
        exit = Column(Text, nullable=False, default="")

    class Note(_AddOn, Base):
        __tablename__ = "notes"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False)
        note = Column(Text, nullable=False)

#        room = relationship("Room", uselist=False,
#            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Skiplist(_AddOn, Base):
        __tablename__ = "skip"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String, nullable=False)
        rooms = relationship("Room", secondary=assoc_skip_room, backref="skiplists")

    class Word(_AddOn, Base):
        __tablename__ = "words"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String, nullable=False, unique=True)
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
        name = Column(String, nullable=False)
        word_id = Column(Integer, ForeignKey("words.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)

        word = relationship("Word", backref=backref("aliases", cascade="all, delete-orphan"))

    class WordRoom(_AddOn, Base):
        __tablename__ = "wordroom"
        id = Column(Integer, nullable=True, primary_key=True)

        word_id = Column(Integer, ForeignKey("words.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)

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
        name = Column(String, nullable=False)
        flag = Column(Integer, nullable=False, default=0)
        step = Column(Integer, nullable=True)

        def id_str(self):
            if self.step:
                return _("‹{self.name}›").format(self=self)
            else:
                return _("‹{self.name}:{self.step}›").format(self=self)

        def add_step(self, **kw):
            step = (session.query(func.max(QuestStep.step)).filter(QuestStep.quest==self).scalar() or 0) +1
            qs = QuestStep(quest_id=self.id, step=step, **kw)
            session.add(qs)
            return qs

        def step_nr(self, nr=None):
            if nr is None:
                nr = self.step
                if nr is None:
                    return None
            return session.query(QuestStep).filter(QuestStep.quest_id == self.id, QuestStep.step==nr).one_or_none()

        @property
        def current_step(self):
            return self.step_nr(None)

    class QuestStep(_AddOn, Base):
        __tablename__ = "queststep"
        id = Column(Integer, nullable=True, primary_key=True)

        quest_id = Column(Integer, ForeignKey("quest.id", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)
        room_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE",ondelete="CASCADE"), nullable=False)
        step = Column(Integer, nullable=False)
        # WARNING quest_id+step should be unique but cannot be because
        # renumbering then won't work because mysql doesn't support
        # deferring unique key checks until commit, and sqlalchemy doesn't
        # support order_by when updating
        command = Column(String, nullable=False)

        flag = Column(Integer, nullable=False, default=0)
        # 1 command, 2 stop execution

        quest = relationship("Quest", backref=backref("steps", cascade="all, delete-orphan"))
        room = relationship("Room", backref=backref("queststeps", cascade="all, delete-orphan"))

        def set_step(self, step):
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

    def get_quest(name):
        q = session.query(Quest).filter(Quest.name == name).one_or_none()
        if q is None:
            raise KeyError(name)
        return q

    def get_feature(name):
        f = session.query(Feature).filter(Feature.name == name).one_or_none()
        if f is None:
            raise KeyError(name)
        return f

    def setup(server):
        session._mud__main = ref(server)

    Session=sessionmaker(bind=engine)
    #conn = await engine.connect()
    session=Session()
    res = attrdict(db=session, q=session.query,
            setup=setup,
            Room=Room, Area=Area, Exit=Exit, Skiplist=Skiplist, Quest=Quest,
            Thing=Thing, Feature=Feature, LongDescr=LongDescr, Note=Note,
            r_hash=r_hash, r_old=r_old, r_mudlet=r_mudlet, r_new=r_new,
            skiplist=get_skiplist, word=get_word, thing=get_thing,
            quest=get_quest, feature=get_feature,
            commit=session.commit, rollback=session.rollback,
            add=session.add, delete=session.delete,
            WF_SCANNED=1, WF_SKIP=2, WF_IMPORTANT=3,
            )
    session._main = ref(res)
    try:
        yield res
    finally:
        session.close()

