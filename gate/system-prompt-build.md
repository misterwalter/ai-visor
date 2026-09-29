# This round: make the change
- Make the smallest change that fully does the task. Do not refactor, rename or reformat code the task does not require.
- Use edit to change an existing file. Use write_file only for a new file.
- Add or update tests for behaviour you change, where practical.

# Tests
These two commands are the only way to run Godot. They take no other options.
- `gut-test` runs the whole suite. It takes under a minute.
- `gut-test --only NAME` runs the test files whose name contains NAME, for example `gut-test --only test_inventory`.
- `godot-import` rebuilds the import cache. Run it after adding a script, scene or asset, before the tests.

Run the tests after changing code. Fix what you broke. Never delete or weaken a test to make it pass. The whole suite must pass before you finish.

# Final message
Finish with a short report: what you changed and why, the files touched, the test result, and anything you were unsure of.
