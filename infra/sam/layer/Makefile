build-QuartzCouncilLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	cp -r python/quartzcouncil "$(ARTIFACTS_DIR)/python/"
	python3 -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/python/" --platform manylinux2014_x86_64 --implementation cp --python-version 3.9 --only-binary=:all: --upgrade
