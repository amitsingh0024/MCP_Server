-- REVAMP B-4 — Index lexical text under BOTH the english and simple dictionaries.
--
-- The original tsvector used only 'simple', which does no stemming and no stopword
-- removal: "treatment of fevers" could not match a passage containing "fever". Switching
-- wholesale to 'english' is not an option either — it would mangle the Devanagari and
-- transliterated Sanskrit that makes up much of this corpus.
--
-- `tsvector || tsvector` concatenates, so the column carries stemmed English tokens AND
-- raw tokens. search_lexical mirrors this with `||` on tsquery (which is OR), so an
-- English query matches via the stemmed side and a Devanagari term matches verbatim via
-- the simple side. Cost is roughly a doubled GIN index.

drop index if exists idx_chunks_tsv;
alter table chunks drop column if exists tsv;

alter table chunks add column tsv tsvector generated always as (
    to_tsvector('english', coalesce(text_content, '') || ' ' || coalesce(summary, ''))
    ||
    to_tsvector('simple',  coalesce(text_content, '') || ' ' || coalesce(summary, ''))
) stored;

create index if not exists idx_chunks_tsv on chunks using gin(tsv);
