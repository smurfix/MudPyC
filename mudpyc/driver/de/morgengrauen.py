from . import Driver as _Driver

class Driver(_Driver):
    """
    """
    name = "Morgengrauen"

    def init_std_dirs(self): 
        """
        Im Morgengrauen kann man stellenweise nach nordostunten gehen.
        """
        super().init_std_dirs()
        for a in "nord","sued","":
            for b in "ost","west","":
                for c in "ob","unt","":
                    self._std_dirs.add(a+b+c+"en")
        self._std_dirs.remove("en")  # :-)

    def init_reversal(self):
        """
        nordostunten … suedwestoben.
        """
        self._loc2rev = {}
        r=(("nord","sued"),("ost","west"),("oben","unten"))
        for x in self._std_dirs:
            y=x
            for a,b in r:
                if a in y:
                    y = y.replace(a,b)
                else:
                    y = y.replace(b,a)

            if x!=y and y in self._std_dirs:
                self._loc2rev[x] = y
                self._loc2rev[y] = x
