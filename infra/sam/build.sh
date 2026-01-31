#!/bin/bash
# Build script for QuartzCouncil SAM deployment
# Run from infra/sam directory: ./build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== QuartzCouncil SAM Build ==="
echo "Project root: $PROJECT_ROOT"
echo "SAM dir: $SCRIPT_DIR"

# Clean previous layer python directory
echo ""
echo "Cleaning layer/python..."
rm -rf "$SCRIPT_DIR/layer/python"
mkdir -p "$SCRIPT_DIR/layer/python"

# Copy quartzcouncil package to layer
echo "Copying quartzcouncil package to layer..."
cp -r "$PROJECT_ROOT/src/quartzcouncil" "$SCRIPT_DIR/layer/python/"

echo ""
echo "Layer contents:"
find "$SCRIPT_DIR/layer/python" -type f -name "*.py" | head -20

echo ""
echo "=== Build prep complete ==="
echo ""
echo "Next steps:"
echo "  1. cd $SCRIPT_DIR"
echo "  2. sam build"
echo "  3. sam deploy --guided"
