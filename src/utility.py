"""Private profiles and utility arithmetic (section 7 / 8.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src import config

ISSUES = ("price", "delivery_days", "revisions")
_DIRECTIONS = ("min", "max")
_WEIGHT_TOLERANCE = 1e-6


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Issue:
    direction: str
    ideal: int
    reservation: int
    weight: float

    def norm(self, value: int) -> float:
        raw = (value - self.reservation) / (self.ideal - self.reservation)
        return max(0.0, min(1.0, raw))


@dataclass(frozen=True)
class Profile:
    name: str
    issues: dict[str, Issue]
    beta: float

    @classmethod
    def load(cls, data: dict[str, Any]) -> Profile:
        if not isinstance(data, dict):
            raise ValueError("profile must be an object")
        raw_issues = data.get("issues")
        if not isinstance(raw_issues, dict):
            raise ValueError("profile.issues must be an object")

        issues: dict[str, Issue] = {}
        total_weight = 0.0
        for name in ISSUES:
            raw = raw_issues.get(name)
            if not isinstance(raw, dict):
                raise ValueError(f"missing issue {name}")
            direction = raw.get("direction")
            if direction not in _DIRECTIONS:
                raise ValueError(f"issue {name}: direction must be min or max")
            ideal = raw.get("ideal")
            reservation = raw.get("reservation")
            weight = raw.get("weight")
            if not isinstance(ideal, int) or isinstance(ideal, bool):
                raise ValueError(f"issue {name}: ideal and reservation must be ints")
            if not isinstance(reservation, int) or isinstance(reservation, bool):
                raise ValueError(f"issue {name}: ideal and reservation must be ints")
            if not isinstance(weight, int | float) or isinstance(weight, bool):
                raise ValueError(f"issue {name}: weight must be a number")
            if ideal == reservation:
                raise ValueError(f"issue {name}: ideal must differ from reservation")
            if direction == "min" and ideal > reservation:
                raise ValueError(f"issue {name}: min direction needs ideal < reservation")
            if direction == "max" and ideal < reservation:
                raise ValueError(f"issue {name}: max direction needs ideal > reservation")
            weight_f = float(weight)
            if weight_f < 0:
                raise ValueError(f"issue {name}: weight must be >= 0")
            total_weight += weight_f
            issues[name] = Issue(direction, int(ideal), int(reservation), weight_f)

        if abs(total_weight - 1.0) > _WEIGHT_TOLERANCE:
            raise ValueError(f"weights must sum to 1 (got {total_weight})")

        beta = data.get("beta", config.BETA_DEFAULT)
        if not isinstance(beta, int | float) or isinstance(beta, bool) or beta <= 0:
            raise ValueError("beta must be a positive number")

        return cls(name=str(data.get("name", "profile")), issues=issues, beta=float(beta))

    def _check_terms(self, terms: dict[str, Any]) -> None:
        if not isinstance(terms, dict):
            raise ValueError("terms must be an object")
        for name in ISSUES:
            if name not in terms:
                raise ValueError(f"terms missing {name}")
            if not _is_int(terms[name]):
                raise ValueError(f"terms.{name} must be a strict int")

    def utility(self, terms: dict[str, Any]) -> float:
        self._check_terms(terms)
        return sum(
            self.issues[name].weight * self.issues[name].norm(int(terms[name]))
            for name in ISSUES
        )

    def issue_utilities(self, terms: dict[str, Any]) -> dict[str, float]:
        self._check_terms(terms)
        return {
            name: self.issues[name].weight * self.issues[name].norm(int(terms[name]))
            for name in ISSUES
        }

    def feasible(self, terms: dict[str, Any]) -> bool:
        self._check_terms(terms)
        for name in ISSUES:
            issue = self.issues[name]
            value = int(terms[name])
            if issue.direction == "min" and value > issue.reservation:
                return False
            if issue.direction == "max" and value < issue.reservation:
                return False
        return True

    def target_utility(self, round_number: int) -> float:
        """Boulware concession curve: START_U down to END_U at round T."""

        t = max(0, min(int(round_number), config.MAX_ROUNDS))
        fraction = t / config.MAX_ROUNDS
        return config.START_U - (config.START_U - config.END_U) * (fraction**self.beta)


def load_profile(path: str) -> Profile:
    import json
    from pathlib import Path

    return Profile.load(json.loads(Path(path).read_text(encoding="utf-8")))
