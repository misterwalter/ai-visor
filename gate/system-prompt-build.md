# This round: make the change
- Make the smallest change that fully does the task. Do not refactor, rename or reformat code the task does not require.
- Change an existing file with the edit tool. Write a whole file only when it is new.
- Add or update tests for behaviour you change, where practical.

# Godot 4, not Godot 3
This project is written for Godot 4. Much of what is written about Godot describes Godot 3, and its syntax no longer compiles:
- `setget` is gone. Write `var speed : float = 0.25: set = set_speed`.
- `export var` and `onready var` are now `@export var` and `@onready var`.
- `yield(...)` is now `await`.
- `connect("signal", self, "_method")` is now `signal_name.connect(_method)`.
When an error surprises you, look at how the project's own scripts do the same thing, and copy that.

# Checking your work
These commands are the only way to run Godot. They take no other options.
- `godot-check FILE` compiles one script and prints its errors with file and line, for example `godot-check scripts/player.gd`. Use it after every edit to a script. It takes a few seconds.
- `gut-test --only NAME` runs the test files whose name contains NAME, for example `gut-test --only test_inventory`.
- `gut-test` runs the whole suite. It takes under a minute.
- `godot-import` rebuilds the import cache. Run it after adding a script, scene or asset, before the tests.

When a test run fails, narrow it with `gut-test --only` and read the first error before changing anything. Run the tests after changing code. Fix what you broke. Never delete or weaken a test to make it pass. The whole suite must pass before you finish.

# Final message
Finish with a short report: what you changed and why, the files touched, the test result, and anything you were unsure of.
