-- Ranking v2: jobs sort on a fixed order of keys (relevance tier, soft preferences met, date)
-- instead of one blended score. `priority` stays for old stores and is no longer written.
ALTER TABLE fits ADD COLUMN tier TEXT CHECK (tier IN ('strong', 'possible', 'weak'));
ALTER TABLE fits ADD COLUMN soft_matches INTEGER;   -- Nice preferences met (net of misses)
ALTER TABLE fits ADD COLUMN rank INTEGER;           -- 1 = first; NULL when gated out
ALTER TABLE fits ADD COLUMN rank_reason TEXT;       -- "Strong match · Remote ✓ · posted 2 days ago"
CREATE INDEX fits_rank ON fits(rank);
