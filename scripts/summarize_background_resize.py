#!/usr/bin/env python3
"""Summarize the complete 32-render Milestone 1.3 batch and print report tables.

Timing and CPU aggregates sum the four resize-heavy workload medians. Memory
and disk aggregates take the maximum observed peak, never a sum of peaks.
Two trials per variant are required; raw trials and sample deviations are kept.
"""
import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'docs/benchmarks/milestone13-render.json')
    parser.add_argument('--result', type=Path, default=ROOT / 'docs/benchmarks/milestone13-summary.json')
    args = parser.parse_args()
    args.result.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.input.read_text())
    assert len(data['runs']) == data['expected_runs'] == 32, 'Expected a complete 32-render milestone batch'
    assert all(data['encoded_file_equality_by_case'].values())
    assert all(run['verification']['passed'] for run in data['runs'])
    fields = ['render_wall_seconds', 'resize_process_cpu_seconds',
              'fitted_frame_wall_seconds', 'fitted_frame_process_cpu_seconds',
              'peak_tree_rss_bytes', 'peak_tree_pss_bytes', 'peak_intermediate_bytes',
              'preprocessing_seconds']
    cases = {}
    for name in dict.fromkeys(run['case'] for run in data['runs']):
        cases[name] = {}
        for variant in ['baseline', 'optimized']:
            runs = sorted([run for run in data['runs'] if run['case'] == name and run['variant'] == variant], key=lambda run: run['trial'])
            assert len(runs) == 2
            values = {field: [run[field] for run in runs] for field in fields}
            for category in ['python', 'ffmpeg']:
                values[f'{category}_cpu_seconds'] = [run['process_cpu_seconds_by_category'][category] for run in runs]
            cases[name][variant] = {
                'metrics': {field: {'trials': samples, 'median': statistics.median(samples),
                                    'min': min(samples), 'max': max(samples),
                                    'sample_standard_deviation': statistics.stdev(samples)}
                            for field, samples in values.items()},
                'encoded_file_sha256': runs[0]['video_sha256'],
                'output_bytes': runs[0]['video_bytes'],
                'pss_complete_samples': sum(sample['pss_bytes'] is not None for run in runs for sample in run['resource_samples']),
                'resource_sample_count': sum(len(run['resource_samples']) for run in runs),
            }

    heavy = [name for name in cases if name.endswith('resize-heavy')]
    aggregates = {}
    for field in fields + ['python_cpu_seconds', 'ffmpeg_cpu_seconds']:
        mode = 'max' if field.startswith('peak_') else 'sum_of_case_medians'
        values = {}
        for variant in ['baseline', 'optimized']:
            metrics = [cases[name][variant]['metrics'][field] for name in heavy]
            values[variant] = max(m['max'] for m in metrics) if mode == 'max' else sum(m['median'] for m in metrics)
        values['optimized_minus_baseline'] = values['optimized'] - values['baseline']
        values['percent_change'] = 100 * (values['optimized'] / values['baseline'] - 1) if values['baseline'] else None
        values['aggregation'] = mode
        aggregates[field] = values

    summary = {'baseline_sha256': data['baseline_sha256'], 'optimized_sha256': data['optimized_sha256'],
               'run_count': len(data['runs']), 'all_outputs_equal': True,
               'aggregation_note': 'Timing/CPU aggregate sums the four resize-heavy workload medians. Memory/disk aggregate is maximum observed peak across those trials, not a sum of peaks. Two trials per version; standard deviation uses sample denominator.',
               'cases': cases, 'resize_heavy_aggregate': aggregates}
    args.result.write_text(json.dumps(summary, indent=2) + '\n')
    print('WALL TABLE')
    print('| Workload | Baseline trials s | Optimized trials s | Median reduction |')
    print('|---|---:|---:|---:|')
    for name, case in cases.items():
        a, b = [case[v]['metrics']['render_wall_seconds'] for v in ['baseline', 'optimized']]
        trials = lambda m: ' / '.join(f'{value:.2f}' for value in m['trials'])
        print(f'| {name} | {trials(a)} | {trials(b)} | {100*(1-b["median"]/a["median"]):.2f}% |')
    print('\nAGGREGATE TABLE')
    print('| Metric | Baseline | Optimized | Difference (O − B) |')
    print('|---|---:|---:|---:|')
    for field in ['render_wall_seconds', 'python_cpu_seconds', 'ffmpeg_cpu_seconds',
                  'peak_tree_rss_bytes', 'peak_tree_pss_bytes', 'fitted_frame_wall_seconds',
                  'fitted_frame_process_cpu_seconds', 'resize_process_cpu_seconds',
                  'peak_intermediate_bytes', 'preprocessing_seconds']:
        m = aggregates[field]
        divisor = 1048576 if field.startswith('peak_') else 1
        pct = f' ({m["percent_change"]:+.2f}%)' if m['percent_change'] is not None else ''
        print(f'| {field} | {m["baseline"]/divisor:.3f} | {m["optimized"]/divisor:.3f} | {m["optimized_minus_baseline"]/divisor:+.3f}{pct} |')
    print('\nMEMORY TABLE')
    print('| Workload | Baseline peak RSS MiB | Optimized peak RSS MiB | Baseline peak PSS MiB | Optimized peak PSS MiB |')
    print('|---|---:|---:|---:|---:|')
    for name, case in cases.items():
        vals = [case[v]['metrics'][field]['max']/1048576 for field in ['peak_tree_rss_bytes', 'peak_tree_pss_bytes'] for v in ['baseline','optimized']]
        print('| ' + name + ' | ' + ' | '.join(f'{value:.2f}' for value in vals) + ' |')
    print('\nPSS coverage', sum(c[v]['pss_complete_samples'] for c in cases.values() for v in c), '/', sum(c[v]['resource_sample_count'] for c in cases.values() for v in c))


if __name__ == '__main__':
    main()
