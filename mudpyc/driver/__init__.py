# generic driver

from typing import NewType, Optional, Dict, Set, Tuple, Final, Union

LocalDir = NewType("LocalDir", str)
ShortDir = NewType("ShortDir", str)
IntlDir = NewType("IntlDir", str)

class Driver:
	name = "Generic driver"

	lang:str = None # should be overridden

	# Used by `init_std_dirs` but Mudlet-specific so not modifyable
	_dir_intl: Final[Tuple[IntlDir, ...]] = \
			tuple("north south east west northeast northwest southeast southwest up down in out".split())

	# These data are used by `init_std_dirs` and may be overridden
	_dir_local: Tuple[LocalDir, ...] = _dir_intl
	_dir_short: Tuple[ShortDir, ...] = \
			tuple("n s e w ne nw se sw u d i o".split())
	_rev_pairs: Tuple[Tuple[LocalDir,LocalDir], ...] = \
			(("north","south"),("east","west"),("up","down"),("in","out"))

	# Reverse a direction. Filled by `init_std_dirs`.
	# May contain non-standard directions.
	_loc2rev: Dict[LocalDir,LocalDir] = None

	# Mappings between _dir_intl and _dir_local, filled in by `init_std_dirs`
	_loc2intl: Dict[LocalDir,IntlDir] = None
	_intl2loc: Dict[IntlDir,LocalDir] = None
	_short2loc: Dict[ShortDir,LocalDir] = None
	# cache
	_std_dirs: Set[Union[LocalDir,ShortDir]] = None
	_intl_dirs: Set[IntlDir] = None

	def is_mudlet_dir(d: IntlDir) -> bool:
		"""
		Return a flag whether this is a standard Mudlet direction.

		TODO: backend specific, except that we don't have multiple backends yet.
		"""
		return d in self._intl_dirs

	def short2loc(self, d: str) -> LocalDir:
		"""
		Translate a short direction, as understood by your MUD, to
		the equivalent long-hand version. E.g. "n" ⟶ "north".

		Short directions are *not* recognized by any other method in this class.

		Any unknown direction is returned unmodified.
		"""
		return self._short2loc.get(d,d)

	@staticmethod
	def intl2loc(self, d: IntlDir) -> LocalDir:
		"""
		Translate an international direction, as understood by Mudlet, to
		the name understood by the MUD you're talking to.

		This is the inverse of `intl2loc`.

		Any unknown direction is returned unmodified.
		"""
		return self._intl2loc.get(d,d)

	@staticmethod
	def loc2intl(self, d: LocalDir) -> IntlDir:
		"""
		Translate a direction as understood by the MUD to the name required
		for Mudlet's 12 standardized directions.

		This is the inverse of `intl2loc`.

		Any unknown direction is returned unmodified.
		"""
		return self._loc2intl.get(d,d)

	def loc2rev(self, d: LocalDir) -> Optional[LocalDir]:
		"""
		Reverse a direction. Typically uses `_loc2rev` but might recognize
		other and/or non-reversible exits, i.e. "enter ‹anything›" might
		map to an unspecific "leave".

		Returns `None` if not reversible.
		"""
		return self._loc2rev.get(d, None)

	def is_std_dir(self, d: LocalDir) -> bool:
		"""
		Returns `True` if the direction is commonly understood by the MUD
		as some sort of movement.
		"""
		return d in self._std_dirs

	def init_std_dirs(self):
		"""
		Setup method to configure the standard directions.

		This method builds _std_dirs from _dir_local.
		"""
		self._std_dirs = set(self._dir_local)

	def offset_delta(self, d: LocalDir):
		"""
		When walking in direction X this function returns a 5-tuple in which
		direction the room is. Each item is either -1, 0, or 1. At least
		one item is != 0.

		dx: east/west
		dy: north/south
		dz: up/down
		ds: in/out
		dt: something else (never -1)
		"""
		return self.offset_delta_intl(self.loc2intl(d))

	def offset_delta_intl(self, d: LocalDir):
		"""
		Default implementation for offset_delta
		"""
		dx,dy,dz,ds,dt = 0,0,0,0,0
		if d == "up":
			dz = 1
		elif d == "down":
			dz = -1
		elif d == "in":
			ds = 1
		elif d == "out":
			ds = -1
		else:
			if "north" in d:
				dy = 1
			elif "south" in d:
				dy = 1
			if "east" in d:
				dx = 1
			elif "west" in d:
				dx = 1
			if (dx,dy) == (0,0):
				dt = 1
		return dx,dy,dz,ds,dt



	def init_short2loc(self):
		"""
		Setup method to configure the standard directions.

		This method builds _short2loc from _dir_local and _dir_short.
		"""
		self._short2loc = { s:l for s,l in zip(self._dir_short,self._dir_local) }

	def init_intl(self):
		"""
		Setup method to configure local/international translation.

		This method builds _loc2intl and _intl2loc from _dir_local and _dir_intl.
		"""
		self._loc2intl = {}
		self._intl2loc = {}
		for k,v in zip(self._dir_local, self._dir_intl):
			self._loc2intl[k] = v
			self._intl2loc[v] = k
		self._intl_dirs = set(self._dir_intl)

	def init_reversal(self):
		"""
		Setup method to configure direction reversal.

		This method builds _loc2rev from _dir_local and _rev_pairs.

		Must be called after `init_intl`.
		"""
		rp = self._rev_pairs
		if self._rev_pairs == Driver._rev_pairs and self._dir_local != self._dir_intl:
			# translated names but untranslated pairs
			rp = ((self.intl2loc(a),self.intl2loc(b)) for a,b in rp)
		self._loc2rev = {}
		for x,y in rp:
			self._loc2rev[x] = y
			self._loc2rev[y] = x

	def __init__(self, cfg:dict):
		self.init_std_dirs()
		self.init_short2loc()
		self.init_intl()
		self.init_reversal()
	
