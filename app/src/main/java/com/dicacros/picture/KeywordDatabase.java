package com.dicacros.picture;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

final class KeywordDatabase extends SQLiteOpenHelper {

    private static final String DB_NAME = "picture_keywords.db";
    private static final int DB_VERSION = 2;

    KeywordDatabase(Context context) {
        super(context.getApplicationContext(), DB_NAME, null, DB_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase database) {
        database.execSQL("CREATE TABLE keyword_history ("
                + "keyword TEXT PRIMARY KEY COLLATE NOCASE,"
                + "sources TEXT NOT NULL DEFAULT '',"
                + "best_rank INTEGER NOT NULL DEFAULT 999,"
                + "first_seen INTEGER NOT NULL,"
                + "last_seen INTEGER NOT NULL,"
                + "selected INTEGER NOT NULL DEFAULT 0,"
                + "auto_selected INTEGER NOT NULL DEFAULT 0,"
                + "excluded INTEGER NOT NULL DEFAULT 0,"
                + "selected_at INTEGER NOT NULL DEFAULT 0,"
                + "seen_count INTEGER NOT NULL DEFAULT 1,"
                + "interest_score INTEGER NOT NULL DEFAULT 0,"
                + "use_count INTEGER NOT NULL DEFAULT 0,"
                + "last_used INTEGER NOT NULL DEFAULT 0)");
        database.execSQL("CREATE INDEX keyword_last_seen_idx "
                + "ON keyword_history(last_seen DESC)");
        database.execSQL("CREATE INDEX keyword_rotation_idx "
                + "ON keyword_history(selected, use_count, last_used, selected_at)");
        database.execSQL("CREATE TABLE related_keyword ("
                + "seed TEXT NOT NULL COLLATE NOCASE,"
                + "keyword TEXT NOT NULL COLLATE NOCASE,"
                + "source TEXT NOT NULL DEFAULT '',"
                + "fetched_at INTEGER NOT NULL,"
                + "use_count INTEGER NOT NULL DEFAULT 0,"
                + "last_used INTEGER NOT NULL DEFAULT 0,"
                + "PRIMARY KEY(seed, keyword))");
        database.execSQL("CREATE INDEX related_seed_idx ON related_keyword(seed)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase database, int oldVersion, int newVersion) {
        if (oldVersion < 2) {
            database.execSQL(
                    "ALTER TABLE keyword_history ADD COLUMN auto_selected INTEGER NOT NULL DEFAULT 0");
            database.execSQL(
                    "ALTER TABLE keyword_history ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0");
            database.execSQL(
                    "ALTER TABLE keyword_history ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 1");
            database.execSQL(
                    "ALTER TABLE keyword_history ADD COLUMN interest_score INTEGER NOT NULL DEFAULT 0");
            database.execSQL(
                    "ALTER TABLE related_keyword ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0");
            database.execSQL(
                    "ALTER TABLE related_keyword ADD COLUMN last_used INTEGER NOT NULL DEFAULT 0");
        }
    }

    void upsertRankings(List<RankedKeyword> rankings) {
        if (rankings == null || rankings.isEmpty()) {
            return;
        }
        SQLiteDatabase database = getWritableDatabase();
        long now = System.currentTimeMillis();
        database.beginTransaction();
        try {
            for (RankedKeyword item : rankings) {
                String keyword = normalizeKeyword(item.keyword);
                if (!isUsableKeyword(keyword)) {
                    continue;
                }
                Existing existing = readExisting(database, keyword);
                if (existing == null) {
                    ContentValues values = new ContentValues();
                    values.put("keyword", keyword);
                    values.put("sources", item.source);
                    values.put("best_rank", item.rank);
                    values.put("first_seen", now);
                    values.put("last_seen", now);
                    database.insertWithOnConflict(
                            "keyword_history", null, values, SQLiteDatabase.CONFLICT_IGNORE);
                } else {
                    ContentValues values = new ContentValues();
                    values.put("sources", mergeSources(existing.sources, item.source));
                    values.put("best_rank", Math.min(existing.bestRank, item.rank));
                    values.put("last_seen", now);
                    values.put("seen_count", existing.seenCount + 1);
                    database.update("keyword_history", values, "keyword=?",
                            new String[]{keyword});
                }
            }
            database.setTransactionSuccessful();
        } finally {
            database.endTransaction();
        }
    }

    void addManualKeyword(String rawKeyword) {
        String keyword = normalizeKeyword(rawKeyword);
        if (!isUsableKeyword(keyword)) {
            return;
        }
        upsertRankings(java.util.Collections.singletonList(
                new RankedKeyword(keyword, "직접입력", 0)));
        setSelected(keyword, true);
    }

    List<KeywordRecord> loadKeywords(int limit) {
        List<KeywordRecord> records = new ArrayList<>();
        int safeLimit = Math.max(1, Math.min(1000, limit));
        try (Cursor cursor = getReadableDatabase().query(
                "keyword_history",
                new String[]{"keyword", "sources", "best_rank", "first_seen",
                        "last_seen", "selected", "auto_selected", "excluded",
                        "seen_count", "interest_score", "use_count", "last_used"},
                null, null, null, null,
                "(selected OR auto_selected) DESC, interest_score DESC, "
                        + "last_seen DESC, best_rank ASC, keyword ASC",
                String.valueOf(safeLimit))) {
            while (cursor.moveToNext()) {
                records.add(readRecord(cursor));
            }
        }
        return records;
    }

    List<KeywordRecord> loadSelectedKeywords() {
        List<KeywordRecord> records = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().query(
                "keyword_history",
                new String[]{"keyword", "sources", "best_rank", "first_seen",
                        "last_seen", "selected", "auto_selected", "excluded",
                        "seen_count", "interest_score", "use_count", "last_used"},
                "(selected=1 OR auto_selected=1) AND excluded=0",
                null, null, null,
                "selected DESC, interest_score DESC, selected_at ASC, keyword ASC")) {
            while (cursor.moveToNext()) {
                records.add(readRecord(cursor));
            }
        }
        return records;
    }

    void setSelected(String rawKeyword, boolean selected) {
        String keyword = normalizeKeyword(rawKeyword);
        if (keyword.isEmpty()) {
            return;
        }
        ContentValues values = new ContentValues();
        values.put("selected", selected ? 1 : 0);
        values.put("auto_selected", 0);
        values.put("excluded", selected ? 0 : 1);
        values.put("selected_at", selected ? System.currentTimeMillis() : 0);
        getWritableDatabase().update(
                "keyword_history", values, "keyword=?", new String[]{keyword});
    }

    void setAllSelected(List<String> rawKeywords, boolean selected) {
        SQLiteDatabase database = getWritableDatabase();
        database.beginTransaction();
        try {
            for (String rawKeyword : rawKeywords) {
                String keyword = normalizeKeyword(rawKeyword);
                if (keyword.isEmpty()) {
                    continue;
                }
                ContentValues values = new ContentValues();
                values.put("selected", selected ? 1 : 0);
                values.put("auto_selected", 0);
                values.put("excluded", selected ? 0 : 1);
                values.put("selected_at", selected ? System.currentTimeMillis() : 0);
                database.update("keyword_history", values, "keyword=?",
                        new String[]{keyword});
            }
            database.setTransactionSuccessful();
        } finally {
            database.endTransaction();
        }
    }

    void saveRelated(RelatedKeywordFetcher.Result result) {
        if (result == null) {
            return;
        }
        String seed = normalizeKeyword(result.seed);
        if (seed.isEmpty()) {
            return;
        }
        SQLiteDatabase database = getWritableDatabase();
        long now = System.currentTimeMillis();
        database.beginTransaction();
        try {
            saveRelatedSource(database, seed, "네이버", result.naver, now);
            saveRelatedSource(database, seed, "다음", result.daum, now);
            saveRelatedSource(database, seed, "구글", result.google, now);
            database.setTransactionSuccessful();
        } finally {
            database.endTransaction();
        }
    }

    List<RelatedRecord> loadRelated(String rawSeed) {
        String seed = normalizeKeyword(rawSeed);
        List<RelatedRecord> records = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().query(
                "related_keyword",
                new String[]{"keyword", "source", "fetched_at", "use_count", "last_used"},
                "seed=?", new String[]{seed}, null, null,
                "use_count ASC, last_used ASC, source ASC, keyword ASC")) {
            while (cursor.moveToNext()) {
                records.add(new RelatedRecord(
                        cursor.getString(0), cursor.getString(1), cursor.getLong(2),
                        cursor.getInt(3), cursor.getLong(4)));
            }
        }
        return records;
    }

    List<String> loadAllRelatedForSelected() {
        Set<String> keywords = new LinkedHashSet<>();
        for (KeywordRecord selected : loadSelectedKeywords()) {
            for (RelatedRecord related : loadRelated(selected.keyword)) {
                keywords.add(related.keyword);
            }
        }
        return new ArrayList<>(keywords);
    }

    KeywordBundle nextKeywordBundle() {
        String keyword = null;
        try (Cursor cursor = getReadableDatabase().query(
                "keyword_history",
                new String[]{"keyword"},
                "(selected=1 OR auto_selected=1) AND excluded=0",
                null, null, null,
                "use_count ASC, last_used ASC, interest_score DESC, "
                        + "selected_at ASC, keyword ASC",
                "1")) {
            if (cursor.moveToFirst()) {
                keyword = cursor.getString(0);
            }
        }
        if (keyword == null || keyword.isEmpty()) {
            return null;
        }
        List<RelatedRecord> records = loadRelated(keyword);
        Set<String> related = new LinkedHashSet<>();
        int minimumUseCount = Integer.MAX_VALUE;
        for (RelatedRecord record : records) {
            related.add(record.keyword);
            minimumUseCount = Math.min(minimumUseCount, record.useCount);
        }
        List<String> relatedList = new ArrayList<>(related);
        List<String> focusCandidates = new ArrayList<>();
        for (RelatedRecord record : records) {
            if (record.useCount == minimumUseCount) {
                focusCandidates.add(record.keyword);
            }
        }
        String focus = KeywordInterestScorer.chooseFocus(keyword, focusCandidates);
        return new KeywordBundle(keyword, focus, relatedList);
    }

    void markUsed(String rawKeyword) {
        markUsed(rawKeyword, "");
    }

    void markUsed(String rawKeyword, String rawFocus) {
        String keyword = normalizeKeyword(rawKeyword);
        if (keyword.isEmpty()) {
            return;
        }
        long now = System.currentTimeMillis();
        getWritableDatabase().execSQL(
                "UPDATE keyword_history "
                        + "SET use_count=use_count+1,last_used=? WHERE keyword=?",
                new Object[]{now, keyword});
        String focus = normalizeKeyword(rawFocus);
        if (!focus.isEmpty() && !focus.equalsIgnoreCase(keyword)) {
            getWritableDatabase().execSQL(
                    "UPDATE related_keyword SET use_count=use_count+1,last_used=? "
                            + "WHERE seed=? AND keyword=?",
                    new Object[]{now, keyword, focus});
        }
    }

    int refreshAutomaticSelections(int limit) {
        List<AutoCandidate> candidates = new ArrayList<>();
        for (KeywordRecord record : loadKeywords(1000)) {
            List<String> related = new ArrayList<>();
            for (RelatedRecord relatedRecord : loadRelated(record.keyword)) {
                related.add(relatedRecord.keyword);
            }
            int score = KeywordInterestScorer.score(
                    record.keyword, record.sources, record.seenCount,
                    record.bestRank, related);
            if (!record.excluded && KeywordInterestScorer.isEvergreenCandidate(
                    record.keyword, record.sources, record.seenCount,
                    record.bestRank, related)) {
                candidates.add(new AutoCandidate(record.keyword, score));
            }
            ContentValues scoreValue = new ContentValues();
            scoreValue.put("interest_score", Math.max(0, score));
            getWritableDatabase().update(
                    "keyword_history", scoreValue, "keyword=?",
                    new String[]{record.keyword});
        }
        Collections.sort(candidates,
                Comparator.comparingInt((AutoCandidate value) -> value.score).reversed()
                        .thenComparing(value -> value.keyword));
        int selectedLimit = Math.max(1, Math.min(30, limit));
        SQLiteDatabase database = getWritableDatabase();
        database.beginTransaction();
        try {
            database.execSQL("UPDATE keyword_history SET auto_selected=0");
            int selected = 0;
            for (AutoCandidate candidate : candidates) {
                if (selected >= selectedLimit) {
                    break;
                }
                ContentValues values = new ContentValues();
                values.put("auto_selected", 1);
                values.put("interest_score", candidate.score);
                database.update("keyword_history", values,
                        "keyword=? AND excluded=0", new String[]{candidate.keyword});
                selected++;
            }
            database.setTransactionSuccessful();
            return selected;
        } finally {
            database.endTransaction();
        }
    }

    void clearAutomaticSelections() {
        getWritableDatabase().execSQL(
                "UPDATE keyword_history SET auto_selected=0");
    }

    int keywordCount() {
        return scalarCount("SELECT COUNT(*) FROM keyword_history");
    }

    int selectedCount() {
        return scalarCount("SELECT COUNT(*) FROM keyword_history "
                + "WHERE (selected=1 OR auto_selected=1) AND excluded=0");
    }

    int automaticSelectedCount() {
        return scalarCount("SELECT COUNT(*) FROM keyword_history "
                + "WHERE auto_selected=1 AND selected=0 AND excluded=0");
    }

    int relatedCount() {
        return scalarCount("SELECT COUNT(*) FROM related_keyword");
    }

    static String normalizeKeyword(String value) {
        if (value == null) {
            return "";
        }
        return Normalizer.normalize(value, Normalizer.Form.NFC)
                .replaceAll("\\s+", " ")
                .trim();
    }

    static boolean isUsableKeyword(String keyword) {
        if (keyword == null || keyword.length() < 2 || keyword.length() > 100) {
            return false;
        }
        String compact = keyword.replace(" ", "");
        if (compact.matches("20\\d{2}년\\d{1,2}월\\d{1,2}일.*")
                || compact.matches("\\d{1,2}시\\d{1,2}분기준")
                || compact.matches("20\\d{2}년\\d{1,2}월")
                || compact.matches("\\d{1,2}월\\d{1,2}일.*기준")) {
            return false;
        }
        String lower = keyword.toLowerCase(Locale.ROOT);
        return !lower.equals("기준") && !lower.contains("copyright");
    }

    private Existing readExisting(SQLiteDatabase database, String keyword) {
        try (Cursor cursor = database.query(
                "keyword_history", new String[]{"sources", "best_rank", "seen_count"},
                "keyword=?", new String[]{keyword}, null, null, null, "1")) {
            if (cursor.moveToFirst()) {
                return new Existing(
                        cursor.getString(0), cursor.getInt(1), cursor.getInt(2));
            }
        }
        return null;
    }

    private void saveRelatedSource(SQLiteDatabase database, String seed, String source,
                                   List<String> values, long now) {
        for (String value : values) {
            String keyword = normalizeKeyword(value);
            if (!isUsableKeyword(keyword) || keyword.equalsIgnoreCase(seed)) {
                continue;
            }
            ContentValues insert = new ContentValues();
            insert.put("seed", seed);
            insert.put("keyword", keyword);
            insert.put("source", source);
            insert.put("fetched_at", now);
            long row = database.insertWithOnConflict(
                    "related_keyword", null, insert, SQLiteDatabase.CONFLICT_IGNORE);
            if (row < 0) {
                ExistingRelated existing = readExistingRelated(database, seed, keyword);
                ContentValues update = new ContentValues();
                update.put("source", mergeSources(existing.source, source));
                update.put("fetched_at", now);
                database.update("related_keyword", update,
                        "seed=? AND keyword=?", new String[]{seed, keyword});
            }
        }
    }

    private ExistingRelated readExistingRelated(
            SQLiteDatabase database, String seed, String keyword) {
        try (Cursor cursor = database.query(
                "related_keyword", new String[]{"source"},
                "seed=? AND keyword=?", new String[]{seed, keyword},
                null, null, null, "1")) {
            if (cursor.moveToFirst()) {
                return new ExistingRelated(cursor.getString(0));
            }
        }
        return new ExistingRelated("");
    }

    private String mergeSources(String existing, String incoming) {
        Set<String> sources = new LinkedHashSet<>();
        if (existing != null) {
            for (String value : existing.split(",")) {
                if (!value.trim().isEmpty()) {
                    sources.add(value.trim());
                }
            }
        }
        if (incoming != null && !incoming.trim().isEmpty()) {
            sources.add(incoming.trim());
        }
        return BlogGenerator.join(new ArrayList<>(sources), ",");
    }

    private KeywordRecord readRecord(Cursor cursor) {
        return new KeywordRecord(
                cursor.getString(0),
                cursor.getString(1),
                cursor.getInt(2),
                cursor.getLong(3),
                cursor.getLong(4),
                cursor.getInt(5) == 1,
                cursor.getInt(6) == 1,
                cursor.getInt(7) == 1,
                cursor.getInt(8),
                cursor.getInt(9),
                cursor.getInt(10),
                cursor.getLong(11));
    }

    private int scalarCount(String sql) {
        try (Cursor cursor = getReadableDatabase().rawQuery(sql, null)) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    static final class RankedKeyword {
        final String keyword;
        final String source;
        final int rank;

        RankedKeyword(String keyword, String source, int rank) {
            this.keyword = normalizeKeyword(keyword);
            this.source = source;
            this.rank = rank;
        }
    }

    static final class KeywordRecord {
        final String keyword;
        final String sources;
        final int bestRank;
        final long firstSeen;
        final long lastSeen;
        final boolean selected;
        final boolean autoSelected;
        final boolean excluded;
        final int seenCount;
        final int interestScore;
        final int useCount;
        final long lastUsed;

        KeywordRecord(String keyword, String sources, int bestRank, long firstSeen,
                      long lastSeen, boolean selected, boolean autoSelected,
                      boolean excluded, int seenCount, int interestScore,
                      int useCount, long lastUsed) {
            this.keyword = keyword;
            this.sources = sources;
            this.bestRank = bestRank;
            this.firstSeen = firstSeen;
            this.lastSeen = lastSeen;
            this.selected = selected;
            this.autoSelected = autoSelected;
            this.excluded = excluded;
            this.seenCount = seenCount;
            this.interestScore = interestScore;
            this.useCount = useCount;
            this.lastUsed = lastUsed;
        }
    }

    static final class RelatedRecord {
        final String keyword;
        final String source;
        final long fetchedAt;
        final int useCount;
        final long lastUsed;

        RelatedRecord(String keyword, String source, long fetchedAt,
                      int useCount, long lastUsed) {
            this.keyword = keyword;
            this.source = source;
            this.fetchedAt = fetchedAt;
            this.useCount = useCount;
            this.lastUsed = lastUsed;
        }
    }

    static final class KeywordBundle {
        final String seed;
        final String focus;
        final List<String> related;

        KeywordBundle(String seed, String focus, List<String> related) {
            this.seed = seed;
            this.focus = focus;
            this.related = related;
        }
    }

    private static final class Existing {
        final String sources;
        final int bestRank;
        final int seenCount;

        Existing(String sources, int bestRank, int seenCount) {
            this.sources = sources;
            this.bestRank = bestRank;
            this.seenCount = seenCount;
        }
    }

    private static final class AutoCandidate {
        final String keyword;
        final int score;

        AutoCandidate(String keyword, int score) {
            this.keyword = keyword;
            this.score = score;
        }
    }

    private static final class ExistingRelated {
        final String source;

        ExistingRelated(String source) {
            this.source = source;
        }
    }
}
