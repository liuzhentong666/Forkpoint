"""Describe observed behavior and partial usage without rerunning an experiment."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean, median

TOKEN_KEYS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')


def analyze(root):
    root = Path(root)
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    trials = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(root.glob('run-*/result.json'))]
    rows = []
    for trial in trials:
        folder = root / f"run-{trial['run']:02d}"
        events = [json.loads(line) for line in (folder/'raw/server.jsonl').read_bytes().splitlines()]
        turns_started = {e['params']['threadId'] for e in events if e.get('method') == 'turn/started'}
        metrics = trial.get('metrics', {})
        known = [t for t in metrics.get('tokens_by_thread', {}).values() if t is not None]
        observed = {key: sum(t[key] for t in known) for key in TOKEN_KEYS}
        terminal_errors = [p['turn']['error'] for p in trial['phases'] if p['turn'].get('error')]
        sqlstates = Counter(c['sqlstate'] for c in trial['calls'] if c.get('sqlstate'))
        actions = Counter(c['arguments']['action'] for c in trial['calls'])
        phase_calls = Counter(c['phase'] for c in trial['calls'])
        rows.append({
            'run': trial['run'], 'success': trial['success'], 'status': trial['status'],
            'tool_calls': metrics.get('tool_calls', 0), 'actions': dict(actions),
            'calls_by_phase': dict(phase_calls), 'sqlstates': dict(sqlstates),
            'observed_tokens': observed, 'threads_with_usage': len(known),
            'threads_started_inference': len(turns_started),
            'tokens_complete_for_both_threads': metrics.get('tokens_total') is not None,
            'missing_token_threads': metrics.get('missing_token_threads', []),
            'terminal_errors': terminal_errors,
            'unexpected_helper_errors': [c for c in trial['calls'] if c.get('error') and
                                         c.get('sqlstate') not in ('40001', 'P0001')],
            'stale_then_semantic_conflict': [c.get('sqlstate') for c in trial['calls'] if c.get('sqlstate')] == ['40001', 'P0001'],
        })
    completed = [r for r in rows if r['status'] == 'completed']
    calls = [r['tool_calls'] for r in completed]
    token_counts = [r['observed_tokens']['totalTokens'] for r in completed if r['tokens_complete_for_both_threads']]
    def distribution(values):
        return {'min': min(values), 'max': max(values), 'mean': mean(values), 'median': median(values)} if values else None
    result = {
        'basis': 'Derived offline from retained result.json and raw server.jsonl; no model self-grading.',
        'planned_runs': summary['planned_runs'], 'recorded_runs': len(rows),
        'completed_runs': len(completed), 'successful_runs': sum(r['success'] for r in rows),
        'runs_with_usage_limit_error': [r['run'] for r in rows if any(e.get('codexErrorInfo') == 'usageLimitExceeded' for e in r['terminal_errors'])],
        'observed_tokens_all_runs': {key: sum(r['observed_tokens'][key] for r in rows) for key in TOKEN_KEYS},
        'usage_scope': 'Sum only reported per-thread cumulative counters once. Partial runs are included; unreported usage is unknown, not estimated or counted as zero.',
        'completed_run_tool_distribution': distribution(calls),
        'completed_run_token_distribution': distribution(token_counts),
        'completed_runs_with_both_conflicts': [r['run'] for r in completed if r['stale_then_semantic_conflict']],
        'runs_with_unexpected_helper_errors': [r['run'] for r in rows if r['unexpected_helper_errors']],
        'rows': rows,
    }
    (root/'behavior-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    result=analyze(parser.parse_args().directory)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
