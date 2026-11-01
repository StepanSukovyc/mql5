import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from benchmark_ollama import action, main, validate_prediction


class BenchmarkTests(unittest.TestCase):
    def test_validation_and_ties(self):
        prediction = {"BUY": 40, "SELL": 20, "HOLD": 40, "reasoning": "test"}
        self.assertEqual(validate_prediction(json.dumps({"TEST": prediction}), "TEST"), prediction)
        self.assertEqual(action(prediction), "TIE")
        for bad in ({"BUY": True}, {"BUY": 101}, {"BUY": 10}, {"reasoning": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_prediction(json.dumps(prediction | bad), "TEST")

    def test_main_sends_identical_prompts_and_saves_separate_results(self):
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            return httpx.Response(200, json={"response": json.dumps({
                "TEST": {"BUY": 60, "SELL": 20, "HOLD": 20, "reasoning": "test"},
            })})

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "TEST.json").write_text(json.dumps({
                "symbol": "TEST", "current_price": 1,
                "candles": {"1h": [{"close": 1}]}, "oscillators": {},
            }))
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch("benchmark_ollama.__file__", str(root / "benchmark_ollama.py")), \
                 patch("benchmark_ollama._load_dotenv"), \
                 patch("benchmark_ollama.get_ollama_cloud_api_key", return_value="test-key"), \
                 patch("benchmark_ollama.get_ollama_cloud_url", return_value="https://example.invalid/api/generate"), \
                 patch("benchmark_ollama.get_ollama_cloud_num_ctx", return_value=16384), \
                 patch("benchmark_ollama.httpx.Client", return_value=client), \
                 patch("sys.argv", ["benchmark", "--source", str(source), "--limit", "1"]):
                self.assertEqual(main(), 0)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["prompt"], requests[1]["prompt"])
            self.assertEqual({r["model"] for r in requests}, {"gpt-oss:120b", "qwen3.8:27b"})
            result = next((root / "benchmarks").glob("*/summary.json"))
            self.assertEqual(json.loads(result.read_text())["direction_agreements"], 1)
            self.assertEqual(len(list(source.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
