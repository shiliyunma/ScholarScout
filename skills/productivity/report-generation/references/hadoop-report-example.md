# Report Generation — Session Reference

## File Structure (from Hadoop 实验报告)

```
D:\大三下\云计算\大作业\
├── Hadoop_云环境报告.md              ← Markdown source
├── Hadoop_云环境报告.docx            ← Final .docx output
├── 实验报告_执行计划.md              ← Plan/requirements doc
├── 实验问题回顾与踩坑总结.md         ← Issues log
├── plantuml_src/                     ← .puml source files
│   ├── cluster_architecture.puml
│   ├── experiment_workflow.puml
│   └── bottleneck_breakdown.puml
├── plantuml_output/                  ← Rendered PNGs
│   ├── cluster_architecture.png
│   ├── experiment_workflow.png
│   └── bottleneck_breakdown.png
├── python_output/                    ← Data charts (copied from ~/hadoop-cluster/)
├── chart_results/                    ← Existing charts (symlinked/copied)
├── 大作业模板_转换.docx              ← Template converted .doc → .docx
└── 大作业模板 - 副本.doc            ← Original template (.doc format)

C:\Users\Lenovo\hadoop-cluster\       ← Data & scripts
├── experiment_summary_detailed.csv   ← Core data (30 rows x 8 cols)
├── experiment_summary.csv            ← Summary (31 rows x 3 cols)
├── benchmark_results_*/              ← 31 subdirectories
│   └── test_summary.csv, mr_counters.csv, realtime_metrics.csv
├── chart_results/                    ← Pre-generated 4 charts
│   ├── 01_各参数折线图.png
│   ├── 02_Top3对比.png
│   ├── 03_交互效应.png
│   └── 04_参数敏感性.png
├── generate_report_charts.py         ← Python data chart script
└── build_report_docx.py             ← docx builder script
```

## Pipeline Script Pattern

Two-phase approach:

### Phase 1: Data Processing (generate_report_charts.py)
- Reads CSV data from experiment_summary_detailed.csv
- Reads per-run counters from benchmark_results_*/mr_counters.csv
- Reads realtime metrics from benchmark_results_*/realtime_metrics.csv
- Generates matplotlib charts → python_output/*.png

### Phase 2: Document Building (build_report_docx.py)
- Loads template .docx (converted from .doc via win32com)
- Writes sections with proper formatting
- Inserts images (plantuml_output/, python_output/, chart_results/)
- Creates formatted tables
- Saves final .docx

## Common Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| PlantUML + Chinese path | "Error line N" garbled | Copy .puml to /tmp/ first |
| .doc not readable | PackageNotFoundError | Convert via win32com first |
| Chinese quotes in Python | SyntaxError | Use single quotes or Unicode escapes |
| Template lacks Table Grid | KeyError no style | Remove `.style = 'Table Grid'` |
| CSV merge by grep -v | Only last entry persisted | Read all entries first, write together |
| String sorting param values | 128m < 32m in x-axis | Implement parse_value() for numeric sort |
