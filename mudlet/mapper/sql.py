from mudlet.util import attrdict, combine_dict
from contextlib import contextmanager
from weakref import ref
from heapq import heappush,heappop

from sqlalchemy import ForeignKey, Column, Integer, MetaData, Table, String, Text, create_engine, select, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, object_session, validates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable, DropTable

from .const import SignalThis, SkipRoute, SkipSignal
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED

import gettext
_ = gettext.gettext

class NoData(RuntimeError):
    def __str__(self):
        if self.args:
            return _("‹NoData:{':'.join(self.args)}›").format(self=self)
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

    class Area(_AddOn, Base):
        __tablename__ = "area"
        id = Column(Integer, primary_key=True)
        name = Column(String)
        rooms = relationship("Room", back_populates="area")

    class Exit(_AddOn, Base):
        __tablename__ = "exits"
        id = Column(Integer, primary_key=True)
        src_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False)
        dir = Column(String, nullable=False)
        dst_id = Column(Integer, ForeignKey("rooms.id_old", onupdate="CASCADE", ondelete="RESTRICT"), nullable=True)
        det = Column(Integer, nullable=False, default=0) # ways with multiple destinations ## TODO
        cost = Column(Integer, nullable=False, default=1)
        steps = Column(String, nullable=True)

        src = relationship("Room", back_populates="_exits", foreign_keys="Exit.src_id")
        dst = relationship("Room", foreign_keys="Exit.dst_id")

        @validates("dir")
        def dir_min_len(self, key, dir) -> str:
            if len(dir) < 3:  # oben
                raise ValueError('some_string too short')
            return dir

        @property
        def info_str(self):
            if self.dst_id:
                res = _("Exit: {self.src.id_str} via {self.dir} {self.dst.id_str}").format(self=self)
            else:
                res = _("Exit: {self.src.id_str} via {self.dir}").format(self=self)
            if self.steps:
                res += _(" ({lsm})").format(lsm=len(self.moves))
            return "‹"+res+"›"

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
        hash_mg = Column(Integer, nullable=True)
        name = Column(String)
        label = Column(String)
        # long_descr = Column(Text)
        pos_x = Column(Integer)
        pos_y = Column(Integer)
        pos_z = Column(Integer)
        area_id = Column(Integer, ForeignKey("area.id", onupdate="CASCADE",ondelete="RESTRICT"), nullable=True)

        # when changing an area
        orig_area = None
        info_area = None

        area = relationship(Area, back_populates="rooms")
        _exits = relationship(Exit,
            primaryjoin=id_old == Exit.src_id, foreign_keys=[Exit.src_id])

        @property
        def info_str(self):
            return _("‹Room: {self.id_str} {self.exit_str} {self.name}›").format(self=self)

        @property
        def id_str(self):
            old = str(self.id_old) if self.id_old else ""
            mud = str(self.id_mudlet) if self.id_mudlet else ""
            return f"{old}/{mud}"

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
                    d += "="+str(x.dst_id)
                    if x.dst.id_mudlet is None:
                        d += "-*"
                ex.append(d)
            return ":"+":".join(ex)+":"

        @property
        def exits(self):
            res = {}
            for x in self._exits:
                res[x.dir] = x.dst
            return res

        @property
        def long_descr(self):
            d = self._long_descr
            if d is None:
                return None
            return d.descr

        @long_descr.setter
        def long_descr(self, descr):
            d = self._long_descr
            if d is None:
                d = Descr(descr=descr, room_id=self.id_old)
                self._s.add(d)
            else:
                d.descr = descr

        @long_descr.deleter
        def long_descr(self):
            if self._long_descr is not None:
                self._s.delete(self._long_descr)

        @property
        def note(self):
            d = self._note
            if d is None:
                return None
            return d.descr

        @note.setter
        def note(self, descr):
            d = self._note
            if d is None:
                d = Descr(descr=descr, room_id=self.id_old)
                self._s.add(d)
            else:
                d.descr = descr

        @note.deleter
        def note(self):
            if self._note is not None:
                self._s.delete(self._note)

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

        async def set_area(self, area):
            mud = self._m.mud

            if self.area == area:
                return
            self.area = area
            if self.id_mudlet:
                await mud.setRoomArea(self.id_mudlet,area.name)

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
            This iterator returns room IDs reachable from this one.

            TODO use a cache to speed up that nonsense.
            """
            res = [(0,[self.id_old])]
            seen=set()
            c=None
            while res:
                d,h = heappop(res)
                r = h[-1]
                if r in seen:
                    continue
                if seen:
                    c = (yield h)
                seen.add(r)
                if c is SkipRoute or c is SkipSignal:
                    continue
                if c is StopIteration:
                    return
                for x in session.query(Exit).filter(Exit.src_id==r):
                    if x.dst_id is None:
                        continue
                    heappush(res,(d+x.cost, h[:]+[x.dst_id]))

        def exit_at(self,d):
            """
            Return the exit(s) in a specific direction
            """
            res = []
            for x in self._exits:
                if x.dir == d:
                    res.append(x)
            if not res:
                raise KeyError()
            if len(res) == 1:
                return res[0]
            return res

        def exit_to(self,d):
            """
            Return the exit(s) to a specific room
            """
            res = []
            if isinstance(d,Room):
                d = d.id_old
            for x in self._exits:
                if x.dst_id == d:
                    res.append(x)
            if not res:
                raise KeyError()
            if len(res) == 1:
                return res[0]
            return res

        async def set_exit(self,d,v=True,skip_mud=False):
            """
            Add an exit d going to room v.
            v=True (default): add but don't overwrite if it points somewhere.
            v=None: exit goes nowhere known
            """
            # TODO split this up
            x = self.exits
            if v is True:
                for x in self._exits:
                    if x.dir == d:
                        break
                else:
                    x = Exit(src=self, dir=d)
                    self._s.add(x)
            else:
                for x in self._exits:
                    if x.dir == d:
                        if v:
                            x.dst = v
                        else:
                            self._s.delete(x)
                        break
                else:
                    if v:
                        x = Exit(src=self, dir=d, dst=v)
                        self._s.add(x)

            if not skip_mud:
                await self.set_mud_exit(d,v)
            #self._exits = ":".join(f"{k}={v}" if v else f"{k}" for k,v in x.items())
            self._s.commit()

            return x

        async def del_exit(self,d, skip_mud=False):
            await self.set_exit(d,None, skip_mud=skip_mud)

        @property
        async def mud_exits(self):
            m = self._m
            mud = m.mud
            res = {}
            if not self.id_mudlet:
                raise NoData
            x = (await mud.getRoomExits(self.id_mudlet))[0]
            y = (await mud.getSpecialExitsSwap(self.id_mudlet))[0]
            return combine_dict(m.itl2loc(x),y)

        async def set_mud_exit(self,d,v=True):
            m = self._m
            mud = self._m.mud

            d = m.loc2itl(d)
            if v and v is not True:  # set
                if d in m.itl_names:
                    await mud.setExit(self.id_mudlet,v.id_mudlet,d)
                else:
                    await mud.addSpecialExit(self.id_mudlet,v.id_mudlet,d)
            elif not v:  # delete
                if d in m.itl_names:
                    await mud.setExit(self.id_mudlet, -1, d)
                else:
                    await mud.removeSpecialExit(self.id_mudlet,d)
            else:
                x = (await mud.getRoomExits(self.id_mudlet))[0]
                if d not in x:
                    if d in m.itl_names:
                        await mud.setExitStub(self.id_mudlet, d, True)
                    else: # special exit
                        await mud.addSpecialExit(self.id_mudlet,0,d) # ?

            await mud.setRoomEnv(self.id_mudlet, ENV_OK+self.open_exits)

    class LongDescr(_AddOn, Base):
        __tablename__ = "longdescr"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("room.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False)
        descr = Column(Text, nullable=False)

        room = relationship("Room", backref="_longdescr", uselist=False,
            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Note(_AddOn, Base):
        __tablename__ = "notes"
        id = Column(Integer, nullable=True, primary_key=True)
        room_id = Column(Integer, ForeignKey("room.id_old", onupdate="CASCADE",ondelete="RESTRICT"), nullable=False)
        note = Column(Text, nullable=False)

        room = relationship("Room", backref="_note", uselist=False,
            primaryjoin=id == Room.id_old, foreign_keys=[Room.id_old])

    class Skiplist(_AddOn, Base):
        __tablename__ = "skip"
        id = Column(Integer, nullable=True, primary_key=True)
        name = Column(String, nullable=False)
        rooms = relationship("Room", secondary=assoc_skip_room, backref="skiplists")

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
        res = session.query(Room).filter(Room.hash_mg == room).one_or_none()
        if res is None:
            raise NoData("id_hash",room)
        return res

    def r_new():
        res=Room()
        session.add(res)
        return res

    def r_skiplist(name, create=None):
        sk = session.query(Skiplist).filter(Skiplist.name == name).one_or_none()
        if sk is None:
            if create is False:
                raise NoData("name",name)
            sk = Skiplist(name=name)
            session.add(sk)
        return sk

    def setup(server):
        session._mud__main = ref(server)

    Session=sessionmaker(bind=engine)
    #conn = await engine.connect()
    session=Session()
    res = attrdict(db=session, q=session.query,
            setup=setup,
            Room=Room, Area=Area, Exit=Exit, Skiplist=Skiplist,
            r_hash=r_hash, r_old=r_old, r_mudlet=r_mudlet,
            r_new=r_new, r_skiplist=r_skiplist,
            commit=session.commit, rollback=session.rollback, add=session.add)
    session._main = ref(res)
    try:
        yield res
    finally:
        session.close()

