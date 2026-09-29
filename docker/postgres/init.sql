-- Runs once on first start of the postgres volume.
CREATE EXTENSION IF NOT EXISTS vector;
-- Separate database for the optional Letta server (profile "letta"), keeps its tables apart from Atlas.
CREATE DATABASE letta;
\c letta
CREATE EXTENSION IF NOT EXISTS vector;
