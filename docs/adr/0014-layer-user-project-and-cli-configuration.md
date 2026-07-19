# Layer user, project, and CLI configuration

Accessibilizer will read user defaults from `~/.config/accessibilizer/config.toml`, then an optional project-local `accessibilizer.toml`, with CLI flags taking highest precedence. API secrets will never be stored in TOML and will instead be referenced by environment-variable name; interactive first-run setup may create user defaults, while noninteractive runs must supply every missing value declaratively.
