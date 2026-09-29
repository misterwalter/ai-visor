extends SceneTree
## Loads one script the way the running project would, autoloads and all, and says
## whether it compiled.
##
## Godot's own --check-only is no use for this: it parses before the project's
## autoloads exist, so it fails a good script that names one and can exit 0 on a
## script that did not compile.
##
##   godot --headless --path <project> -s <this file> -- res://path/to/script.gd
##
## Exit status: 0 compiled, 1 did not, 2 called wrongly. The engine prints the
## errors themselves, with file and line, above the verdict.


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 1:
		printerr("check_script: expected one script path, got %d" % args.size())
		quit(2)
		return
	var path := args[0]
	if not FileAccess.file_exists(path):
		print("CHECK FAILED: no such file: %s" % path)
		quit(1)
		return
	# Read from disk, never from a cache: the file has usually just been edited.
	var script := ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_IGNORE) as Script
	if script == null or not _compiled(script):
		print("CHECK FAILED: %s did not compile. The errors are above." % path)
		quit(1)
		return
	print("CHECK PASSED: %s compiles." % path)
	quit(0)


## A script that did not compile is still handed back by the loader, as an empty
## shell, so being handed one proves nothing. One that can be instantiated did
## compile. One that cannot is either broken or abstract, and compiling it again
## is the call that says which.
func _compiled(script: Script) -> bool:
	return script.can_instantiate() or script.reload() == OK
