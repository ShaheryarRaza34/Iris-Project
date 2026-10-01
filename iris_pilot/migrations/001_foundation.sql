-- Schemas: staging (raw, loosely typed), core (promoted, contract-enforced),
-- mart (derived screening views), ops (orchestration metadata).
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;
