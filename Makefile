
python.mpackage:
	cd mpackage && zip ../python.mpackage config.lua python.xml

trans:
	pygettext3 -d pymudlet -o locales/pymudlet.pot mudlet/alias.py mudlet/__init__.py mudlet/server.py mudlet/util.py mudlet/mapper/const.py mudlet/mapper/__init__.py mudlet/mapper/main.py mudlet/mapper/sql.py mudlet/mapper/walking.py

trans.de:	trans
	msgmerge -U locales/de/LC_MESSAGES/pymudlet.po locales/pymudlet.pot
	msgfmt -o locales/de/LC_MESSAGES/pymudlet.mo locales/de/LC_MESSAGES/pymudlet.po
