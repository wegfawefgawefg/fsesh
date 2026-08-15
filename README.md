# fsesh

`fsesh` saves Firefox's current tabs as named JSON files and restores them later.
It reads Firefox's own session snapshot, so saving and listing do not require an
extension. Loading opens the saved URLs in a new Firefox window and leaves
existing tabs alone.

## Install

```sh
git clone https://github.com/wegfawefgawefg/fsesh.git
cd fsesh
python3 -m pip install --user lz4
ln -s "$PWD/fsesh.py" ~/.local/bin/fsesh
```

Make sure `~/.local/bin` is in `PATH`.

## Commands

```sh
fsesh save work
fsesh list
fsesh load work
```

Run `fsesh` with no arguments for a dmenu interface. `fsesh save` and `fsesh
load` also use dmenu when the session name is omitted.

Sessions are stored in `~/.local/share/fsesh/sessions/`. Each one is ordinary
JSON containing the URLs, titles, window grouping, tab order, and pinned state.

## Limitations

- Firefox writes its recovery snapshot periodically. A tab opened immediately
  before `fsesh save` may not have reached the snapshot yet.
- Private-browsing tabs are not stored by Firefox and therefore cannot be saved.
- Version 1 restores all URLs into one new window. The JSON retains window and
  pinned-tab information so a later extension-backed version can restore those
  details exactly.

## Requirements

- Python 3
- Python package `lz4`
- Firefox
- dmenu (only for the graphical menu)
