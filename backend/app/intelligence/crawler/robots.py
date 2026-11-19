"""Compatibility re-exports for the Gate 1B robots policy module."""

from .robots_policy import (
    CRAWLER_USER_AGENT,
    DEFAULT_ROBOTS_POLICY,
    RobotsFormatError,
    RobotsPolicy,
    RobotsPolicyError,
    RobotsRules,
    get_default_robots_policy,
)

__all__ = [
    "CRAWLER_USER_AGENT",
    "DEFAULT_ROBOTS_POLICY",
    "RobotsFormatError",
    "RobotsPolicy",
    "RobotsPolicyError",
    "RobotsRules",
    "get_default_robots_policy",
]
