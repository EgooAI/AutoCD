#!/usr/bin/env bash
# autocd:begin v1
# repository = "git@github.com:Egooai/your-project.git"
# branch = "main"
# interval_minutes = 1
# cooldown_minutes = 5
# timeout_minutes = 15
# autocd:end

set -euo pipefail
cd "$RELEASE_DIR"
printf 'Deploying %s (previous: %s)\n' "$DEPLOY_SHA" "$PREVIOUS_SHA"

# Write the complete build, activation and health-check sequence here.
# PROJECT_DIR points to the original directory; persistent data belongs outside releases.
# The service runs without an interactive login shell. Set PATH/tool versions explicitly.
# Use a service manager to start long-lived services; do not background them with &.
printf 'Replace this example with project-specific deployment commands.\n' >&2
exit 1
