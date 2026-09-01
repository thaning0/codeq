use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Parser;
use serde::Serialize;
use serde_json::{Value, json};

type Result<T> = std::result::Result<T, String>;

#[derive(Debug, Parser)]
#[command(about = "Evaluate CodeQ 2.0 release acceptance artifacts")]
struct Options {
    #[arg(long, value_name = "PATH")]
    performance: PathBuf,

    #[arg(long = "workflow-replay", value_name = "PATH")]
    workflow_replay: PathBuf,

    #[arg(
        long,
        value_name = "PATH",
        default_value = "benchmarks/results/0.5.2-workflows.json"
    )]
    historical: PathBuf,

    #[arg(
        long = "agent-utility",
        value_name = "PATH",
        default_value = "benchmarks/results/1.0.0rc11-agent-utility.json"
    )]
    agent_utility: PathBuf,

    #[arg(long, value_name = "PATH")]
    output: Option<PathBuf>,

    #[arg(long, value_name = "PATH")]
    markdown: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
struct Check {
    name: &'static str,
    passed: bool,
    actual: Value,
    requirement: &'static str,
}

#[derive(Debug, Serialize)]
struct GateResult {
    status: &'static str,
    schema_version: u64,
    checks: Vec<Check>,
    performance_version: String,
    workflow_version: String,
    historical_version: String,
    agent_utility_parser_version: String,
}

fn main() -> ExitCode {
    match run(Options::parse()) {
        Ok(passed) => {
            if passed {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(1)
            }
        }
        Err(error) => {
            eprintln!("codeq readiness gate: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(options: Options) -> Result<bool> {
    let performance = read_json(&options.performance)?;
    let workflow = read_json(&options.workflow_replay)?;
    let historical = read_json(&options.historical)?;
    let utility = read_json(&options.agent_utility)?;
    let checks = evaluate(&performance, &workflow, &historical, &utility);
    let passed = checks.iter().all(|check| check.passed);
    let result = GateResult {
        status: if passed { "PASS" } else { "FAIL" },
        schema_version: 1,
        checks,
        performance_version: string_at(&performance, "/codeq_version"),
        workflow_version: string_at(&workflow, "/codeq_version"),
        historical_version: string_at(&historical, "/codeq_version"),
        agent_utility_parser_version: string_at(&utility, "/parser_version"),
    };
    let rendered = serde_json::to_string_pretty(&result)
        .map_err(|error| format!("cannot serialize gate result: {error}"))?
        + "\n";
    if let Some(path) = options.output {
        write_output(&path, &rendered)?;
    }
    if let Some(path) = options.markdown {
        write_output(&path, &render_markdown(&result))?;
    }
    print!("{rendered}");
    Ok(passed)
}

fn evaluate(
    performance: &Value,
    workflow: &Value,
    historical: &Value,
    utility: &Value,
) -> Vec<Check> {
    let mut checks = Vec::new();
    for (name, pointer, limit, requirement) in [
        (
            "warm_context_p95",
            "/warm/context_symbol/p95_ms",
            3_000.0,
            "<= 3000 ms",
        ),
        (
            "warm_trace_p95",
            "/warm/trace_in/p95_ms",
            3_000.0,
            "<= 3000 ms",
        ),
        (
            "cold_context_p95",
            "/cold/context_symbol/p95_ms",
            5_000.0,
            "<= 5000 ms",
        ),
        (
            "cold_trace_p95",
            "/cold/trace_in/p95_ms",
            5_000.0,
            "<= 5000 ms",
        ),
    ] {
        let actual = number_at(performance, pointer);
        checks.push(Check {
            name,
            passed: actual > 0.0 && actual <= limit,
            actual: json!(actual),
            requirement,
        });
    }

    let semantic_names = BTreeSet::from([
        "find_exact",
        "find_concept",
        "context_symbol",
        "context_reference_store",
        "context_cursor",
        "context_lexical",
        "trace_in",
        "review",
        "review_broad",
    ]);
    let mut semantic_max = 0.0_f64;
    for phase in ["cold", "warm"] {
        let Some(series) = performance.get(phase).and_then(Value::as_object) else {
            continue;
        };
        for (name, result) in series {
            if !semantic_names.contains(name.as_str()) {
                continue;
            }
            for sample in result
                .get("samples")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                semantic_max = semantic_max.max(number_at(sample, "/duration_ms"));
            }
        }
    }
    checks.push(Check {
        name: "no_10s_semantic_outlier",
        passed: semantic_max > 0.0 && semantic_max < 10_000.0,
        actual: json!(round_one(semantic_max)),
        requirement: "< 10000 ms",
    });
    let live_covered = ["context_reference_store", "review_broad"]
        .into_iter()
        .filter(|name| {
            performance.pointer(&format!("/cold/{name}")).is_some()
                && performance.pointer(&format!("/warm/{name}")).is_some()
        })
        .collect::<Vec<_>>();
    checks.push(Check {
        name: "live_workload_cases",
        passed: live_covered.len() == 2,
        actual: json!(live_covered),
        requirement: "context_reference_store and review_broad in cold and warm samples",
    });

    let workflow_queries = integer_at(workflow, "/summary/queries");
    let workflow_pass = number_at(workflow, "/summary/pass_rate_pct");
    let workflow_actionable = number_at(workflow, "/summary/actionable_rate_pct");
    let commands = workflow
        .pointer("/summary/commands")
        .and_then(Value::as_object);
    let command_coverage = ["find", "context", "trace", "review"]
        .into_iter()
        .filter(|command| {
            commands
                .and_then(|value| value.get(*command))
                .and_then(Value::as_u64)
                .is_some_and(|count| count > 0)
        })
        .collect::<Vec<_>>();
    checks.extend([
        Check {
            name: "workflow_replay_success",
            passed: workflow_queries >= 10 && workflow_pass == 100.0,
            actual: json!({"queries": workflow_queries, "pass_rate_pct": workflow_pass}),
            requirement: ">= 10 representative queries and 100% pass",
        },
        Check {
            name: "workflow_replay_actionability",
            passed: workflow_queries >= 10 && workflow_actionable == 100.0,
            actual: json!({"queries": workflow_queries, "actionable_rate_pct": workflow_actionable}),
            requirement: "100% of successful replay queries expose command-specific actionable output",
        },
        Check {
            name: "four_workflow_commands",
            passed: command_coverage.len() == 4,
            actual: json!(command_coverage),
            requirement: "find, context, trace, and review are represented",
        },
    ]);

    let sample_size = integer_at(historical, "/sample/size");
    let mapping_coverage = number_at(historical, "/sample/mapped_call_coverage_pct");
    let (navigation, navigation_fallback) = historical
        .pointer("/sample/workflows")
        .and_then(Value::as_array)
        .map(|workflows| {
            let navigation: Vec<_> = workflows
                .iter()
                .filter(|workflow| {
                    workflow.get("category").and_then(Value::as_str) == Some("navigation")
                })
                .collect();
            let fallback = navigation
                .iter()
                .filter(|workflow| {
                    workflow
                        .get("fallback_families")
                        .and_then(Value::as_array)
                        .is_some_and(|families| !families.is_empty())
                })
                .count();
            (navigation.len() as u64, fallback as u64)
        })
        .unwrap_or_default();
    let validation_queries = integer_at(historical, "/current_validation/queries");
    let validation_ok = integer_at(historical, "/current_validation/statuses/ok");
    let validation_rate = if validation_queries == 0 {
        0.0
    } else {
        round_one(validation_ok as f64 * 100.0 / validation_queries as f64)
    };
    checks.extend([
        Check {
            name: "historical_sample_size",
            passed: sample_size >= 100,
            actual: json!(sample_size),
            requirement: ">= 100 observed workflows",
        },
        Check {
            name: "historical_mapping_coverage",
            passed: mapping_coverage >= 90.0,
            actual: json!(mapping_coverage),
            requirement: ">= 90% direct or approximate mapping coverage",
        },
        Check {
            name: "navigation_fallback_free",
            passed: navigation >= 50 && navigation_fallback == 0,
            actual: json!({"workflows": navigation, "fallback": navigation_fallback}),
            requirement: ">= 50 navigation workflows and 0 unsupported fallback",
        },
        Check {
            name: "historical_query_validation",
            passed: validation_queries >= 30 && validation_rate >= 95.0,
            actual: json!({"queries": validation_queries, "ok_rate_pct": validation_rate}),
            requirement: ">= 30 frozen extracted queries and >= 95% ok",
        },
    ]);

    let utility_events = integer_at(utility, "/corpus/codeq_call_events");
    let utility_queries = integer_at(utility, "/corpus/codeq_queries");
    let paired = integer_at(utility, "/corpus/paired_output_events");
    let limitations = utility
        .get("attribution_limitations")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    let claim_boundary = utility
        .get("claim_boundary")
        .and_then(Value::as_str)
        .unwrap_or_default();
    checks.push(Check {
        name: "agent_utility_observation",
        passed: utility_events >= 99
            && utility_queries >= 153
            && paired == utility_events
            && limitations > 0
            && claim_boundary.contains("not a causal"),
        actual: json!({
            "events": utility_events,
            "queries": utility_queries,
            "paired_outputs": paired,
            "limitations": limitations,
        }),
        requirement: "frozen privacy-preserving observation is complete and retains its non-causal claim boundary",
    });
    checks
}

fn read_json(path: &Path) -> Result<Value> {
    serde_json::from_slice(
        &fs::read(path).map_err(|error| format!("cannot read {}: {error}", path.display()))?,
    )
    .map_err(|error| format!("invalid JSON {}: {error}", path.display()))
}

fn string_at(value: &Value, pointer: &str) -> String {
    value
        .pointer(pointer)
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_owned()
}

fn number_at(value: &Value, pointer: &str) -> f64 {
    value
        .pointer(pointer)
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
}

fn integer_at(value: &Value, pointer: &str) -> u64 {
    value.pointer(pointer).and_then(Value::as_u64).unwrap_or(0)
}

fn round_one(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn render_markdown(result: &GateResult) -> String {
    let mut lines = vec![
        "# CodeQ 2.0 readiness gate".to_owned(),
        String::new(),
        format!("Overall: **{}**", result.status),
        String::new(),
        "| Gate | Result | Actual | Requirement |".to_owned(),
        "| --- | --- | --- | --- |".to_owned(),
    ];
    for check in &result.checks {
        let actual = serde_json::to_string(&check.actual)
            .unwrap_or_else(|_| "\"serialization error\"".to_owned())
            .replace('|', "\\|");
        lines.push(format!(
            "| {} | {} | `{}` | {} |",
            check.name,
            if check.passed { "PASS" } else { "FAIL" },
            actual,
            check.requirement
        ));
    }
    lines.extend([
        String::new(),
        format!(
            "Performance artifact version: `{}`",
            result.performance_version
        ),
        format!("Workflow replay version: `{}`", result.workflow_version),
        format!(
            "Historical mapping version: `{}`",
            result.historical_version
        ),
        format!(
            "Agent utility parser version: `{}`",
            result.agent_utility_parser_version
        ),
        String::new(),
    ]);
    lines.join("\n")
}

fn write_output(path: &Path, contents: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
    }
    fs::write(path, contents).map_err(|error| format!("cannot write {}: {error}", path.display()))
}
