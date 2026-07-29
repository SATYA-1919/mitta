//! Restart policy for the sidecar.
//!
//! Split out from the supervisor so the policy can be tested without spawning
//! processes. The interesting questions — how long to wait, when to give up,
//! when to reset — are arithmetic, and arithmetic tested through a subprocess
//! is arithmetic tested slowly and flakily.

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct RestartPolicy {
    pub base: Duration,
    pub max: Duration,
    /// Give up after this many consecutive failures.
    pub max_attempts: u32,
    /// A run lasting at least this long is treated as a success, resetting the
    /// backoff.
    pub healthy_after: Duration,
}

impl Default for RestartPolicy {
    fn default() -> Self {
        Self {
            base: Duration::from_millis(500),
            max: Duration::from_secs(30),
            max_attempts: 5,
            healthy_after: Duration::from_secs(30),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestartDecision {
    /// Wait, then restart.
    Retry(Duration),
    /// Stop trying. The UI must say so rather than showing a permanent spinner.
    GiveUp,
}

#[derive(Debug, Default)]
pub struct Backoff {
    attempts: u32,
}

impl Backoff {
    pub fn new() -> Self {
        Self::default()
    }

    /// Decide what to do after a run that lasted `ran_for`.
    ///
    /// `ran_for` is what distinguishes "crashed on startup, five times in a
    /// row" from "ran fine for an hour, then died". Counting attempts alone
    /// would treat a long-lived process that eventually crashes as if it were
    /// crash-looping, and would refuse to restart it after five such days.
    pub fn record_exit(&mut self, ran_for: Duration, policy: &RestartPolicy) -> RestartDecision {
        if ran_for >= policy.healthy_after {
            self.attempts = 0;
        }
        self.attempts += 1;

        if self.attempts > policy.max_attempts {
            return RestartDecision::GiveUp;
        }

        // Exponential, capped. Doubling is computed with saturation because
        // `base << 30` overflows a Duration long before it becomes a sensible
        // wait, and an overflow here would wrap to an instant retry.
        let exponent = self.attempts.saturating_sub(1).min(16);
        let scaled = policy
            .base
            .saturating_mul(2u32.saturating_pow(exponent))
            .min(policy.max);
        RestartDecision::Retry(scaled)
    }

    pub fn reset(&mut self) {
        self.attempts = 0;
    }

    pub fn attempts(&self) -> u32 {
        self.attempts
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const INSTANT: Duration = Duration::from_millis(10);

    #[test]
    fn backs_off_exponentially() {
        let policy = RestartPolicy::default();
        let mut backoff = Backoff::new();

        assert_eq!(
            backoff.record_exit(INSTANT, &policy),
            RestartDecision::Retry(Duration::from_millis(500))
        );
        assert_eq!(
            backoff.record_exit(INSTANT, &policy),
            RestartDecision::Retry(Duration::from_secs(1))
        );
        assert_eq!(
            backoff.record_exit(INSTANT, &policy),
            RestartDecision::Retry(Duration::from_secs(2))
        );
    }

    #[test]
    fn caps_the_delay() {
        let policy = RestartPolicy {
            max: Duration::from_secs(2),
            max_attempts: 100,
            ..Default::default()
        };
        let mut backoff = Backoff::new();

        for _ in 0..20 {
            match backoff.record_exit(INSTANT, &policy) {
                RestartDecision::Retry(delay) => assert!(delay <= policy.max),
                RestartDecision::GiveUp => panic!("gave up too early"),
            }
        }
    }

    #[test]
    fn gives_up_after_max_attempts() {
        let policy = RestartPolicy {
            max_attempts: 3,
            ..Default::default()
        };
        let mut backoff = Backoff::new();

        for _ in 0..3 {
            assert!(matches!(
                backoff.record_exit(INSTANT, &policy),
                RestartDecision::Retry(_)
            ));
        }
        assert_eq!(
            backoff.record_exit(INSTANT, &policy),
            RestartDecision::GiveUp
        );
    }

    #[test]
    fn a_long_healthy_run_resets_the_backoff() {
        // A process that ran for an hour and then died is not crash-looping,
        // and must not inherit the penalty of failures from days ago.
        let policy = RestartPolicy {
            max_attempts: 3,
            ..Default::default()
        };
        let mut backoff = Backoff::new();

        backoff.record_exit(INSTANT, &policy);
        backoff.record_exit(INSTANT, &policy);
        backoff.record_exit(INSTANT, &policy);

        assert_eq!(
            backoff.record_exit(Duration::from_secs(3600), &policy),
            RestartDecision::Retry(policy.base),
            "a healthy run should restore the base delay"
        );
    }

    #[test]
    fn a_short_run_does_not_reset() {
        let policy = RestartPolicy::default();
        let mut backoff = Backoff::new();

        backoff.record_exit(INSTANT, &policy);
        let second = backoff.record_exit(Duration::from_secs(1), &policy);

        assert_eq!(second, RestartDecision::Retry(Duration::from_secs(1)));
    }
}
