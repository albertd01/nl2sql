"""Feature flags for agent behaviors, so each Milestone-3 change can be measured in isolation.

All flags off == the Milestone-2 baseline agent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


# Best configuration from Milestone 3 (Gemma, dev split): see README "Agent flags".
RECOMMENDED = "abstain_rule_v2,force_final,self_check"


@dataclass(frozen=True)
class AgentConfig:
    value_check: bool = False   # 3.1 look up values before filtering; run_query flags filter values absent from the column
    force_final: bool = False   # 3.2 warn near the turn limit; last turn may only call final_answer
    abstain_rule: bool = False  # 3.3 explicit rules for when to answer "unanswerable" (and when not to)
    tie_rule: bool = False      # 3.4 top-N questions include ties at the cutoff (dropped: hurt BIRD)
    self_check: bool = False    # 3.5 final SQL reviewed before final_answer is accepted: fails to run? empty?
    tie_check: bool = False     # 3.5 part 2: self-check also flags ORDER BY ... LIMIT cutting through a tie
    abstain_rule_v2: bool = False  # 3.3b refined answerability rules (nonexistent entities, no substitution, advice)

    @classmethod
    def from_flags(cls, spec: str | None) -> "AgentConfig":
        """Parse 'value_check,tie_rule' (or 'recommended' / 'all' / 'none' / '')."""
        names = [f.name for f in fields(cls)]
        if not spec or spec == "none":
            return cls()
        if spec == "recommended":
            spec = RECOMMENDED
        if spec == "all":
            return cls(**{n: True for n in names})
        chosen = [s.strip() for s in spec.split(",") if s.strip()]
        unknown = [s for s in chosen if s not in names]
        if unknown:
            raise ValueError(f"Unknown flag(s) {unknown}. Available: {', '.join(names)}")
        return cls(**{n: True for n in chosen})

    @property
    def label(self) -> str:
        on = [k for k, v in asdict(self).items() if v]
        return "+".join(on) if on else "baseline"

    def to_dict(self) -> dict:
        return asdict(self)
