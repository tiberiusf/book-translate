#!/bin/sh
# Install book-translate as a command, without touching system Python:
#   - creates a private venv in ./venv and installs requirements.txt into it
#   - writes a small wrapper to ~/.local/bin/book-translate
# Re-run any time (e.g. after `git pull`) to refresh dependencies.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || { echo "Python 3.9+ is required (found $(python3 --version 2>&1))." >&2; exit 1; }

[ -d venv ] || python3 -m venv venv
venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/book-translate" <<EOF
#!/bin/sh
exec "$DIR/venv/bin/python" "$DIR/book_translate.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/book-translate"

echo "Installed: $HOME/.local/bin/book-translate"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Add this to ~/.zshrc so the command is found:"
     echo '  export PATH="$HOME/.local/bin:$PATH"' ;;
esac
