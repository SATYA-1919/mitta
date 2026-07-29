//! System metrics, sampled in Rust at 1 Hz (DEC-003).
//!
//! Sampling here rather than in Python removes a hop, a serialisation and a
//! poll loop from the one thing that runs every second for the entire session.
//!
//! **GPU is reported as unavailable, not as a number.** macOS exposes no
//! unprivileged per-process GPU utilisation API on Apple Silicon
//! (`ARCHITECTURE.md` §13). Every "GPU %" in a Mac app is either a private API,
//! a `powermetrics` call needing root, or a guess. Showing `n/a` is the honest
//! option and the UI is built to render it.

use serde::Serialize;
use sysinfo::System;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SystemMetrics {
    pub cpu_percent: f32,
    pub memory_used_bytes: u64,
    pub memory_total_bytes: u64,
    /// Always `None` on macOS. See the module docs.
    pub gpu_percent: Option<f32>,
    pub battery_percent: Option<f32>,
    pub battery_charging: Option<bool>,
}

pub struct MetricsSampler {
    system: System,
}

impl MetricsSampler {
    pub fn new() -> Self {
        let mut system = System::new();
        // First refresh establishes the baseline. CPU usage is a delta between
        // two samples, so the very first reading is always 0 and must not be
        // shown as if it meant the machine is idle.
        system.refresh_cpu_usage();
        system.refresh_memory();
        Self { system }
    }

    pub fn sample(&mut self) -> SystemMetrics {
        self.system.refresh_cpu_usage();
        self.system.refresh_memory();

        SystemMetrics {
            cpu_percent: self.system.global_cpu_usage(),
            memory_used_bytes: self.system.used_memory(),
            memory_total_bytes: self.system.total_memory(),
            gpu_percent: None,
            battery_percent: None,
            battery_charging: None,
        }
    }
}

impl Default for MetricsSampler {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_plausible_memory() {
        let mut sampler = MetricsSampler::new();
        let metrics = sampler.sample();

        assert!(metrics.memory_total_bytes > 0);
        assert!(metrics.memory_used_bytes <= metrics.memory_total_bytes);
    }

    #[test]
    fn cpu_is_a_percentage() {
        let mut sampler = MetricsSampler::new();
        std::thread::sleep(std::time::Duration::from_millis(250));
        let metrics = sampler.sample();

        assert!(metrics.cpu_percent >= 0.0);
        assert!(metrics.cpu_percent <= 100.0 * num_cpus_upper_bound());
    }

    #[test]
    fn gpu_is_absent_rather_than_fabricated() {
        // ARCHITECTURE.md §13. If this ever returns Some(_), a private API or a
        // guess has crept in.
        assert!(MetricsSampler::new().sample().gpu_percent.is_none());
    }

    #[test]
    fn serialises_camel_case_for_the_webview() {
        let json = serde_json::to_value(MetricsSampler::new().sample()).unwrap();
        assert!(json.get("cpuPercent").is_some());
        assert!(json.get("memoryUsedBytes").is_some());
        assert!(json.get("gpuPercent").is_some(), "must be present, as null");
    }

    fn num_cpus_upper_bound() -> f32 {
        256.0
    }
}
