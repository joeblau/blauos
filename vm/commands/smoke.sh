#!/bin/sh
# Exercise real command behavior as the logged-in, unprivileged guest account.
set -eu
LC_ALL=C
export LC_ALL

for tool in ls cat cp mv rm mkdir rmdir ln chmod touch stat du df head tail wc cut tr uniq tee printenv pwd basename dirname mktemp id whoami groups env date ps grep sed comm paste rev fold expand unexpand hostname; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done

work=$(mktemp -d "$HOME/.blauos-command-check.XXXXXXXX")
case "$work" in "$HOME"/.blauos-command-check.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$work"' EXIT HUP INT TERM
mkdir -p "$work/one/sub"
printf 'alpha\nbeta\nbeta\n' > "$work/one/input"
touch "$work/one/.hidden"
ln -s input "$work/one/link"
case "$(ls -1a "$work/one")" in *'.hidden'*'input'*'link'*'sub'*) ;; *) exit 1 ;; esac
[ "$(cat "$work/one/link")" = "$(printf 'alpha\nbeta\nbeta')" ]
cp -R "$work/one" "$work/copy"
mv "$work/copy/input" "$work/copy/moved"
[ ! -e "$work/copy/input" ]
[ "$(cat "$work/copy/moved")" = "$(cat "$work/one/input")" ]
ln "$work/copy/moved" "$work/hardlink"
printf 'gamma\n' >> "$work/hardlink"
[ "$(tail -n 1 "$work/copy/moved")" = gamma ]
chmod 000 "$work/copy/moved"
if cat "$work/copy/moved" > /dev/null 2>&1; then
    printf 'A non-root account bypassed file permissions\n' >&2
    exit 1
fi
chmod 600 "$work/copy/moved"
[ "$(stat -f %z "$work/one/input")" = 16 ]
chmod u+x "$work/copy/moved"
[ -x "$work/copy/moved" ]
chmod u-x "$work/copy/moved"
[ ! -x "$work/copy/moved" ]
mkdir "$work/empty"
rmdir "$work/empty"
[ ! -e "$work/empty" ]
if ls "$work/missing" > /dev/null 2>&1; then exit 1; fi
if rm "$work/missing" > /dev/null 2>&1; then exit 1; fi
printf 'BLAUOS_COMMAND_CHECK files pass\n'

[ "$(head -n 1 "$work/one/input")" = alpha ]
[ "$(tail -n 2 "$work/one/input")" = "$(printf 'beta\nbeta')" ]
set -- $(wc -l < "$work/one/input")
[ "$1" = 3 ]
[ "$(printf 'a:b:c\n' | cut -d : -f 2)" = b ]
[ "$(printf 'abc\n' | tr a-z A-Z)" = ABC ]
[ "$(uniq "$work/one/input")" = "$(printf 'alpha\nbeta')" ]
[ "$(printf 'copied\n' | tee "$work/tee")" = copied ]
[ "$(cat "$work/tee")" = copied ]
[ "$(basename "$work/one/input")" = input ]
[ "$(dirname "$work/one/input")" = "$work/one" ]
[ "$(cd "$work/one" && env pwd -P)" = "$work/one" ]
printf 'alpha\nbeta\n' > "$work/sorted1"
printf 'beta\ngamma\n' > "$work/sorted2"
[ "$(comm -12 "$work/sorted1" "$work/sorted2")" = beta ]
printf 'a\n' > "$work/paste1"
printf 'b\n' > "$work/paste2"
[ "$(paste -d : "$work/paste1" "$work/paste2")" = a:b ]
[ "$(printf 'abc\n' | rev)" = cba ]
[ "$(printf 'abcd\n' | fold -w 2)" = "$(printf 'ab\ncd')" ]
[ "$(printf 'a\tb\n' | expand -t 4)" = 'a   b' ]
[ "$(printf '        x\n' | unexpand -a)" = "$(printf '\tx')" ]
du -k "$work" > "$work/du"
[ -s "$work/du" ]
df -k "$work" > "$work/df"
[ -s "$work/df" ]
printf 'BLAUOS_COMMAND_CHECK text pass\n'

[ "$(id -u)" != 0 ]
[ "$(id -un)" = "$USER" ]
[ "$(id -u "$USER")" = "$(id -u)" ]
[ "$(whoami)" = "$USER" ]
groups "$USER" > "$work/groups"
[ -s "$work/groups" ]
[ "$(printenv HOME)" = "$HOME" ]
[ "$(env BLAUOS_COMMAND_ENV=works /bin/sh -c 'printf "%s" "$BLAUOS_COMMAND_ENV"')" = works ]
case "$(date +%Y)" in [0-9][0-9][0-9][0-9]) ;; *) exit 1 ;; esac
[ "$(date -u -r 0 +%Y)" = 1970 ]
[ "$(date -j -u -f %Y-%m-%d 2000-01-01 +%Y)" = 2000 ]
[ -n "$(hostname)" ]
set -- $(ps -p "$$" -o pid=)
[ "$1" = "$$" ]
printf 'BLAUOS_COMMAND_CHECK identity pass\n'

[ "$(grep -c '^beta$' "$work/one/input")" = 2 ]
if grep -q absent "$work/one/input"; then exit 1; else [ "$?" = 1 ]; fi
grep -r -l '^alpha$' "$work/one" > "$work/grep"
grep -q '/input$' "$work/grep"
[ "$(sed -n 's/^alpha$/changed/p' "$work/one/input")" = changed ]
for option in -J -M -X -Z; do
    if grep "$option" alpha "$work/one/input" > /dev/null 2>&1; then exit 1; else [ "$?" = 2 ]; fi
done
if sed 's/[invalid/replaced/' "$work/one/input" > /dev/null 2>&1; then exit 1; fi
printf 'BLAUOS_COMMAND_CHECK search pass\n'
printf 'BLAUOS_COMMAND_CHECK all pass\n'
