"""Run the configured local model against labeled historical diagnostics."""
import argparse
import json
from pathlib import Path

from .config import load_config
from .llm import LLMRequest, OllamaBackend

DEFAULT_CASES = Path(__file__).resolve().parent.parent / 'tests/fixtures/llm_diagnostics.json'


def matches(case, result):
    return (result.status == 'ok'
            and result.decision['classification'] in case['expected_classifications']
            and result.decision['action'] in case['expected_actions'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='campaign/campaign.yaml')
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    config = load_config(args.config)[0]['llm']
    if config['backend'] != 'ollama':
        parser.error('La suite richiede il backend Ollama.')
    cases = json.loads(args.cases.read_text())
    model = OllamaBackend(config)
    correct = trusted = trusted_wrong = 0
    for case in cases:
        request = LLMRequest('eval-' + case['id'], 'error_classification', case['context'],
                             case['allowed_actions'])
        result = model.analyze(request)
        if result.status != 'ok':
            print(f"ERRORE {case['id']}: modello {result.status}: {result.error}")
            return 2
        valid = matches(case, result)
        confidence = result.decision['confidence']
        is_trusted = confidence >= config['confidence_threshold']
        correct += valid
        trusted += is_trusted
        trusted_wrong += is_trusted and not valid
        mark = 'PASS' if valid else 'FAIL'
        print(f"{mark} {case['id']} — {result.decision['classification']}, "
              f"{result.decision['action']}, confidenza {confidence:.0%}")
        print('  ' + result.decision['reason'].replace('\n', ' '))
    print(f"\nAccuratezza: {correct}/{len(cases)} ({correct / len(cases):.0%})")
    print(f"Sopra soglia {config['confidence_threshold']:.0%}: {trusted}; errori fidati: {trusted_wrong}")
    print('Valutazione operativa: ' + ('PASS' if trusted_wrong == 0 else 'FAIL'))
    return 0 if trusted_wrong == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
