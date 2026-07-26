package com.dicacros.picture;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class KeywordAutomationEngine {

    private KeywordAutomationEngine() {
    }

    static Result enrichAndRecommend(KeywordDatabase database,
                                     List<KeywordDatabase.RankedKeyword> rankings,
                                     int discoveryLimit, int selectionLimit) {
        List<String> seeds = discoverySeeds(database, rankings, discoveryLimit);
        List<RelatedKeywordFetcher.Result> fetched =
                RelatedKeywordFetcher.fetchAll(seeds, 4);
        int relatedCount = 0;
        for (RelatedKeywordFetcher.Result result : fetched) {
            database.saveRelated(result);
            relatedCount += result.all().size();
        }
        int selected = database.refreshAutomaticSelections(selectionLimit);
        return new Result(seeds.size(), relatedCount, selected);
    }

    private static List<String> discoverySeeds(
            KeywordDatabase database,
            List<KeywordDatabase.RankedKeyword> rankings, int limit) {
        Map<String, Candidate> candidates = new LinkedHashMap<>();
        if (rankings != null) {
            for (KeywordDatabase.RankedKeyword ranking : rankings) {
                Candidate candidate = candidates.get(ranking.keyword);
                if (candidate == null) {
                    candidate = new Candidate(ranking.keyword);
                    candidates.put(ranking.keyword, candidate);
                }
                candidate.bestRank = Math.min(candidate.bestRank, ranking.rank);
                candidate.sources.add(ranking.source);
            }
        }
        for (Candidate candidate : candidates.values()) {
            candidate.relatedCount = database.loadRelated(candidate.keyword).size();
        }
        List<Candidate> ordered = new ArrayList<>(candidates.values());
        Collections.sort(ordered, Comparator.comparingInt(Candidate::score).reversed());
        List<String> output = new ArrayList<>();
        for (Candidate candidate : ordered) {
            if (candidate.score() == Integer.MIN_VALUE) {
                continue;
            }
            output.add(candidate.keyword);
            if (output.size() >= Math.max(1, limit)) {
                break;
            }
        }
        return output;
    }

    static final class Result {
        final int seeds;
        final int related;
        final int selected;

        Result(int seeds, int related, int selected) {
            this.seeds = seeds;
            this.related = related;
            this.selected = selected;
        }
    }

    private static final class Candidate {
        final String keyword;
        final Set<String> sources = new LinkedHashSet<>();
        int bestRank = 999;
        int relatedCount;

        Candidate(String keyword) {
            this.keyword = keyword;
        }

        int score() {
            int score = KeywordInterestScorer.discoveryScore(
                    keyword, bestRank, sources.size());
            if (score == Integer.MIN_VALUE) {
                return score;
            }
            return score + (relatedCount == 0 ? 80 : 0);
        }
    }
}
