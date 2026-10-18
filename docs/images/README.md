# Screenshot artwork

The source is the already published `images/Screenshot2.png`, showing the Venus OS companion console's ESS and DVCC settings. The exact console release was not recorded, so this is labelled historical. Inverter Control has no standalone GUI. The renderer crops to the two setting cards (1320 × 720 at x=130, y=180), omitting the adjacent partial card and browser extension overlay. Values are historical examples, not recommended settings.

`venus-ess-dvcc.png` is the retained flat source. Its SHA-256 is
`ec3313b41eda9d47dcd9b5af8bbd149021ad3890ec72ef46579f5176122f90e4`.
The README links the perspective hero to this source for readable detail.

`app-hero.png` applies only a geometric perspective projection, rounded outer
corners and a soft shadow on a neutral background. No interface text, controls,
charts or readings are generated or repainted. This artwork is documentation,
not test or release acceptance evidence.

## Regenerate

Install ImageMagick 7, then run from the repository root:

```sh
bash scripts/render-docs-hero.sh
```

The output is a static 1600 × 1050 PNG. The script checks source dimensions,
uses a temporary directory, strips metadata and replaces the hero only after a
successful render. Same source and ImageMagick version produce identical output;
resampling can differ across versions. Initially rendered with ImageMagick 7.1.2-31.

When replacing a screenshot, use synthetic or explicitly public data, record the
application version, review the complete flat image for private information,
and update this provenance and the README caption before regenerating.
