.PHONY: install uninstall check

# Live activation remains with the Venus OS PackageManager adapter. A DESTDIR
# stages the exact native payload without invoking helpers or changing services.
install:
	@if test -n "$(DESTDIR)"; then python3 install.py install --destdir "$(DESTDIR)"; else bash setup install auto; fi

uninstall:
	@if test -n "$(DESTDIR)"; then python3 install.py uninstall --destdir "$(DESTDIR)"; else bash setup uninstall auto; fi

check:
	bash scripts/ci.sh
