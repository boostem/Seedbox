# The screenshot tests' browser (scripts/screenshot.ts): a pinned Chrome (Playwright's headless shell), Deno and
# fixed fonts (Inter and JetBrains Mono, both in the app's font stacks), so a laptop and CI render the same pixels.
# Changing anything here changes the screenshots: run `UPDATE_SCREENSHOTS=1 deno task screenshot` afterwards.
FROM denoland/deno:bin-2.9.8 AS deno
FROM mcr.microsoft.com/playwright:v1.64.1-noble
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends fonts-inter fonts-jetbrains-mono && rm -rf /var/lib/apt/lists/*
ENV SEEDBOX_SCREENSHOTS_IN_DOCKER=1
