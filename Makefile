
python.mpackage:
	cd mpackage && zip ../python.mpackage config.lua python.xml

trans:
	pygettext3 -d mudpyc -o locales/mudpyc.pot $(shell find mudpyc -name \*.py)

trans.de:	trans
	msgmerge -U locales/de/LC_MESSAGES/mudpyc.po locales/mudpyc.pot
	msgfmt -o locales/de/LC_MESSAGES/mudpyc.mo locales/de/LC_MESSAGES/mudpyc.po

PACKAGE=mudpyc
ifneq ($(wildcard /usr/share/sourcemgr/make/py),)
include /usr/share/sourcemgr/make/py
# availabe via http://github.com/smurfix/sourcemgr
else
%:
	@echo "Please install 'sourcemgr' (http://github.com/smurfix/sourcemgr)."
	@exit 1
endif

