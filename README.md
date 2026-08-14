# todo

A small, single-file CLI for persistent, ordered, nested todos. One todo is current, and completing it advances to the next item or its parent. Data is stored in SQLite.

```sh
printf '%s\n' 'Ship a documented Linux build.' | ./todo.py add "Ship release" --root --switch --body-file -
printf '%s\n' 'Decide flags and defaults.' | ./todo.py add "Plan interface" --body-file -
printf '%s\n' 'Keep parsing separate from output.' | ./todo.py add "Implement CLI" --switch --body-file -
./todo.py add "Parse arguments"
./todo.py add "Format output"
./todo.py show
```

`show` is the normal working view. It expands the path from the root to the current todo, includes details along that path, shows nearby siblings and children, and collapses unrelated work. An `…` marks a hidden body.

```text
[ ] 1a2b3c4 Ship release
│
│   Ship a documented Linux build.
│
├── [ ] 2b3c4d5 Plan interface …
└── [ ] 3c4d5e6 Implement CLI  ← current
    │
    │   Keep parsing separate from output.
    │
    ├── [ ] 4d5e6f7 Parse arguments
    └── [ ] 5e6f7a8 Format output
```

`all` prints the complete tree; `all --details` also prints every body.

Requires Python 3. The database defaults to `~/.local/share/todo/todo.db`; override it with `TODO_DB` or `--db`. Run `./todo.py --help` for all commands.
