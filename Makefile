.PHONY: data run eval test boundary cockpit
data:      ; python3 dataset/generate.py
run:       ; PYTHONPATH=. python3 -m restartos.cli run --auto-approve
abstain:   ; PYTHONPATH=. python3 -m restartos.cli run --hint "ghost xyz" --line "Line 9" --alarm NONE
eval:      ; PYTHONPATH=. python3 -m restartos.cli eval
boundary:  ; PYTHONPATH=. python3 -m restartos.cli boundary-test
test:      ; PYTHONPATH=. python3 tests/test_pipeline.py

serve:     ; PYTHONPATH=. python3 -m restartos.server --port 8000
