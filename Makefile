.PHONY: app app-test app-run

app:
	./scripts/build-app.sh

app-test:
	swift test --package-path app

app-run:
	PKM_BRAIN_REPO_PATH=$(CURDIR) swift run --package-path app PKMBrainApp
