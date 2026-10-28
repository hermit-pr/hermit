# Project policy

- Configuration is loaded through `parse_config`; never read a JSON file
  anywhere else.
- Every public function needs a docstring.
- Secrets never belong in the tree.
