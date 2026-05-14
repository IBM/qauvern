all: lint test

fmt:
    uv run ruff format

lint:
    uv run ruff format --check
    uv run ruff check
    uv run ty check --error-on-warning

test *args:
    uv run pytest {{args}}

pex:
    uvx pex . \
        -c qauvern \
        -o dist/qauvern.pex \
        --python-shebang '/usr/bin/env python3' \
        --venv prepend \
        --platform linux_x86_64-cp-310-cp310 \
        --platform linux_x86_64-cp-311-cp311 \
        --platform linux_x86_64-cp-312-cp312 \
        --platform linux_x86_64-cp-313-cp313 \
        --platform linux_x86_64-cp-314-cp314 \
        --platform macosx_11_0_arm64-cp-310-cp310 \
        --platform macosx_11_0_arm64-cp-311-cp311 \
        --platform macosx_11_0_arm64-cp-312-cp312 \
        --platform macosx_11_0_arm64-cp-313-cp313 \
        --platform macosx_11_0_arm64-cp-314-cp314 \
        --platform macosx_11_0_x86_64-cp-310-cp310 \
        --platform macosx_11_0_x86_64-cp-311-cp311 \
        --platform macosx_11_0_x86_64-cp-312-cp312 \
        --platform macosx_11_0_x86_64-cp-313-cp313 \
        --platform macosx_11_0_x86_64-cp-314-cp314
