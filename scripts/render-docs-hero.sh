#!/usr/bin/env bash
# Project a captured interface; never generate or redraw its UI.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v magick >/dev/null || { echo 'ImageMagick 7 is required.' >&2; exit 1; }
source_image="$root/docs/images/venus-ess-dvcc.png"
[[ "$(magick identify -format '%wx%h' "$source_image")" == '1478x1062' ]] || { echo 'Screenshot dimensions changed; review the projection.' >&2; exit 1; }
render_dir="$(mktemp -d "${TMPDIR:-/tmp}/inverter-control-hero.XXXXXX")"
trap 'rm -rf -- "$render_dir"' EXIT
magick "$source_image" -crop 1320x720+130+180 +repage -alpha on \
  \( -size 1320x720 xc:none -fill white -draw 'roundrectangle 0,0 1319,719 18,18' \) \
  -compose DstIn -composite -compose Over -virtual-pixel transparent -filter Robidoux \
  -define distort:viewport=1600x1050+0+0 -distort Perspective '0,0 145,222 1319,0 1385,147 1319,719 1450,823 0,719 210,898' -strip "$render_dir/panel.png"
magick "$render_dir/panel.png" -fill '#152033' -colorize 100 -channel A -evaluate multiply 0.23 +channel -blur 0x18 -strip "$render_dir/shadow.png"
magick -size 1600x1050 xc:'#eef2f7' "$render_dir/shadow.png" -geometry +0+22 -compose Over -composite \
  "$render_dir/panel.png" -geometry +0+0 -compose Over -composite -strip -define png:exclude-chunk=date,time "PNG24:$render_dir/hero.png"
mv -- "$render_dir/hero.png" "$root/docs/images/app-hero.png"
