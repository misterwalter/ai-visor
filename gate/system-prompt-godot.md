# Godot 4, not Godot 3
This project is written for Godot 4. Much of what is written about Godot describes Godot 3, and its syntax no longer compiles:
- `setget` is gone. Write `var speed : float = 0.25: set = set_speed`.
- `export var` and `onready var` are now `@export var` and `@onready var`.
- `yield(...)` is now `await`.
- `connect("signal", self, "_method")` is now `signal_name.connect(_method)`.
- In a shader, `hint_color` is now `source_color`, and comments are `//`, never `#`.
- A `ShaderMaterial` takes its shader from a file: `material.shader = preload("res://shaders/x.gdshader")`.
When an error surprises you, look at how the project's own scripts do the same thing, and copy that.

# Checking your work
Godot is not installed here and cannot be started directly, so do not look for it. These commands are the only way to run it. They take no other options.
- `godot-check FILE` compiles one script or shader and prints its errors with file and line, for example `godot-check scripts/player.gd` or `godot-check shaders/water.gdshader`. Use it after every edit, on every file you change, not only on new ones. It takes a few seconds.
- `gut-test --only FILE` runs the test files whose name contains FILE, for example `gut-test --only test_inventory`.
- `gut-test --test NAME` runs the test functions whose name contains NAME, in any file, for example `gut-test --test test_student_drops`.
- `gut-test` runs the whole suite. It takes under a minute.
- `godot-import` rebuilds the import cache. Run it after adding a script, scene or asset, before the tests.

When a test run fails, narrow it with `--only` or `--test` and read the first error before changing anything.
When a test you wrote fails, decide first whether the code or the test is wrong. Never change what the owner asked for, such as a speed or a threshold, only to make your own test pass.
