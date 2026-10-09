.PHONY: install uninstall check

# Treat a caller-supplied destination as literal data, including dollar signs.
override DESTDIR := $(value DESTDIR)
export DESTDIR

# Live activation remains with the Venus OS PackageManager adapter. A DESTDIR
# stages the exact native payload without invoking helpers or changing services.
install:
	@if test -n "$$DESTDIR"; then python3 install.py install --destdir "$$DESTDIR"; else bash setup install auto; fi

uninstall:
	@if test -n "$$DESTDIR"; then python3 install.py uninstall --destdir "$$DESTDIR"; else bash setup uninstall auto; fi

check:
	bash scripts/ci.sh
