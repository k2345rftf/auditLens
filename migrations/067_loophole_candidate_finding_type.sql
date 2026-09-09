-- Тип находки кандидата исследования: лазейка или мошенническая схема.
-- Мошенническая схема сохраняется с is_loophole=TRUE по конвенции миграции 066
-- (положительная находка любого типа); finding_type переносит различие до
-- автоимпорта, который ставит записи каталога classification='fraud_scheme'.
--
-- Порядок деплоя: миграция накатывается ДО выкатки кода (на проде миграции
-- ручные), иначе persist находок упадёт на отсутствующей колонке.
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS finding_type TEXT NOT NULL DEFAULT 'loophole';

-- Констрейнт добавляется отдельным идемпотентным блоком: колонка могла быть
-- создана ранее без CHECK (ручной хот-фикс, частичный накат).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'loophole_research_candidate_finding_type_check'
            AND conrelid = 'loophole_research_candidate'::regclass
    ) THEN
        ALTER TABLE loophole_research_candidate
            ADD CONSTRAINT loophole_research_candidate_finding_type_check
            CHECK (finding_type IN ('loophole', 'fraud_scheme'));
    END IF;
END $$;
