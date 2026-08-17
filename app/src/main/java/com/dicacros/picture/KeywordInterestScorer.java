package com.dicacros.picture;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

final class KeywordInterestScorer {

    static final int MIN_AUTO_SCORE = 58;

    private static final String[] QUESTION_SIGNALS = {
            "누구", "왜", "이유", "어떻게", "방법", "차이", "비교", "전망", "가능성",
            "영향", "뜻", "의미", "관계", "논란", "근황", "프로필", "국적", "나이",
            "재산", "가격", "장단점", "추천", "사용법", "원리", "미래", "전략"
    };

    private static final String[] ENTITY_SIGNALS = {
            "외국인", "해외", "미국", "일본", "중국", "유럽", "영국", "프랑스",
            "독일", "캐나다", "호주", "국적", "프로필", "학력", "경력", "소속",
            "대표", "ceo", "기업", "회사", "그룹", "브랜드", "창업", "사업",
            "실적", "전망", "제품", "인물"
    };

    private static final String[] EPHEMERAL_SIGNALS = {
            "경기 결과", "경기결과", "실시간 스코어", "중계 일정", "중계방송",
            "중계", "스코어", "선발 라인업", "오늘 라인업", "경기 하이라이트",
            "하이라이트", "경기 일정", "경기시간", "경기 시간", "연속골",
            "결승골", "득점", "홈런", "완승", "역전승", "무승부",
            "경기 시작", "경기 종료", "기분 좋은 시작",
            "로또 당첨",
            "당첨 번호", "당첨번호", "오늘 날씨", "현재 날씨", "시간별 날씨",
            "실시간 교통", "교통 상황"
    };

    // 스포츠 경기·선수 기록 등 뉴스만 보고 빠지는 스포츠 관련 검색어.
    private static final String[] SPORTS_SIGNALS = {
            "야구", "축구", "농구", "배구", "골프", "테니스", "탁구", "배드민턴",
            "프로야구", "프로축구", "월드컵", "올림픽", "아시안게임", "리그",
            "프리미어리그", "라리가", "분데스리가", "세리에", "챔피언스리그",
            "kbo", "mlb", "nba", "epl", "선발투수", "구원", "세이브", "타율",
            "방어율", "삼진", "볼넷", "실책", "완봉", "완투", "이닝", "홈런",
            "타점", "안타", "선제골", "결승골", "역전골", "자책골", "감독",
            "구단", "이적", "예선", "본선", "4강", "8강", "16강", "준결승",
            "결승전", "연장전", "승부차기", "승부", "우승", "준우승", "패배",
            "친선경기", "국가대표", "손흥민", "오타니", "스포츠", "선수", "대표팀",
            "득점", "승점", "시즌", "토너먼트",
            "챔피언", "fc", "유나이티드", "올스타", "드래프트"
    };

    // 죽음·사고·범죄 등 부정적이고 소모성 뉴스 검색어(수집 금지).
    private static final String[] NEGATIVE_SIGNALS = {
            "사망", "죽음", "숨진", "숨져", "별세", "타계", "부고", "빈소",
            "영결", "발인", "사인", "자살", "극단적", "참사", "화재", "폭발",
            "붕괴", "추락", "침몰", "실종", "납치", "살해", "살인", "피살",
            "흉기", "성폭행", "성추행", "마약", "음주운전", "뺑소니", "참변",
            "비보", "비극", "부상", "중상", "사고사", "변사", "숨졌다"
    };

    private KeywordInterestScorer() {
    }

    static int discoveryScore(String keyword, int bestRank, int sourceCount) {
        if (!KeywordDatabase.isUsableKeyword(keyword) || isEphemeral(keyword)) {
            return Integer.MIN_VALUE;
        }
        int score = Math.max(0, 14 - Math.max(1, bestRank));
        score += Math.max(1, sourceCount) * 8;
        score += Math.min(18, Math.max(0, keyword.length() - 2));
        score += countSignals(keyword, ENTITY_SIGNALS) * 14;
        score += countSignals(keyword, QUESTION_SIGNALS) * 8;
        return score;
    }

    static int score(String keyword, String sources, int seenCount, int bestRank,
                     List<String> relatedKeywords) {
        if (!KeywordDatabase.isUsableKeyword(keyword) || isEphemeral(keyword)) {
            return Integer.MIN_VALUE;
        }
        Set<String> uniqueRelated = new LinkedHashSet<>();
        if (relatedKeywords != null) {
            for (String related : relatedKeywords) {
                String normalized = KeywordDatabase.normalizeKeyword(related);
                if (KeywordDatabase.isUsableKeyword(normalized) && !isEphemeral(normalized)) {
                    uniqueRelated.add(normalized);
                }
            }
        }

        int sourceCount = sourceCount(sources);
        int questionDepth = 0;
        int entityDepth = countSignals(keyword, ENTITY_SIGNALS);
        for (String related : uniqueRelated) {
            questionDepth += Math.min(2, countSignals(related, QUESTION_SIGNALS));
            entityDepth += Math.min(2, countSignals(related, ENTITY_SIGNALS));
        }

        int score = Math.min(40, Math.max(1, seenCount) * 5);
        score += sourceCount * 12;
        score += Math.max(0, 12 - Math.max(1, bestRank));
        score += Math.min(30, uniqueRelated.size() * 3);
        score += Math.min(24, questionDepth * 4);
        score += Math.min(24, entityDepth * 5);
        if (sourceCount >= 2) {
            score += 10;
        }
        if (seenCount >= 3) {
            score += 10;
        }
        if (uniqueRelated.size() >= 6) {
            score += 12;
        }
        return score;
    }

    static boolean isEvergreenCandidate(String keyword, String sources, int seenCount,
                                        int bestRank, List<String> relatedKeywords) {
        int score = score(keyword, sources, seenCount, bestRank, relatedKeywords);
        int sourceCount = sourceCount(sources);
        int entitySignals = countSignals(keyword + " " + join(relatedKeywords), ENTITY_SIGNALS);
        int questionSignals =
                countSignals(keyword + " " + join(relatedKeywords), QUESTION_SIGNALS);
        return score >= MIN_AUTO_SCORE
                && relatedKeywords != null
                && relatedKeywords.size() >= 4
                && (seenCount >= 2 || sourceCount >= 2
                || entitySignals >= 2 || questionSignals >= 3);
    }

    static String chooseFocus(String seed, List<String> relatedKeywords) {
        String best = KeywordDatabase.normalizeKeyword(seed);
        int bestScore = -1;
        if (relatedKeywords == null) {
            return best;
        }
        for (String value : relatedKeywords) {
            String candidate = KeywordDatabase.normalizeKeyword(value);
            if (!KeywordDatabase.isUsableKeyword(candidate) || isEphemeral(candidate)) {
                continue;
            }
            int score = Math.min(30, candidate.length());
            score += countSignals(candidate, QUESTION_SIGNALS) * 12;
            score += countSignals(candidate, ENTITY_SIGNALS) * 8;
            if (!best.isEmpty() && candidate.toLowerCase(Locale.ROOT)
                    .contains(best.toLowerCase(Locale.ROOT))) {
                score += 16;
            }
            if (score > bestScore) {
                best = candidate;
                bestScore = score;
            }
        }
        return best;
    }

    static boolean isEphemeral(String keyword) {
        String lower = keyword == null ? "" : keyword.toLowerCase(Locale.ROOT);
        for (String signal : EPHEMERAL_SIGNALS) {
            if (lower.contains(signal)) {
                return true;
            }
        }
        for (String signal : SPORTS_SIGNALS) {
            if (lower.contains(signal)) {
                return true;
            }
        }
        for (String signal : NEGATIVE_SIGNALS) {
            if (lower.contains(signal)) {
                return true;
            }
        }
        if (lower.matches(".*\\d+\\s*(이닝|홈런|타점|안타|득점|k).*")
                || lower.matches(".*\\d+경기\\s*연속.*")
                || lower.matches(".*\\b(vs|대)\\b.*\\d+\\s*[-:]\\s*\\d+.*")) {
            return true;
        }
        boolean matchPhrase = lower.contains(" 대 ") || lower.contains(" vs ");
        boolean teamSignal = lower.matches(
                ".*(\\bfc\\b|시티|유나이티드|리버풀|선덜랜드|기아|한화|두산|"
                        + "롯데|삼성|키움|\\bnc\\b|\\bkt\\b).*");
        return matchPhrase && teamSignal;
    }

    static int sourceCount(String sources) {
        Set<String> unique = new LinkedHashSet<>();
        if (sources != null) {
            for (String source : sources.split(",")) {
                if (!source.trim().isEmpty()) {
                    unique.add(source.trim());
                }
            }
        }
        return unique.size();
    }

    private static int countSignals(String value, String[] signals) {
        String lower = value == null ? "" : value.toLowerCase(Locale.ROOT);
        int count = 0;
        for (String signal : signals) {
            if (lower.contains(signal)) {
                count++;
            }
        }
        return count;
    }

    private static String join(List<String> values) {
        if (values == null || values.isEmpty()) {
            return "";
        }
        StringBuilder output = new StringBuilder();
        for (String value : values) {
            output.append(value).append(' ');
        }
        return output.toString();
    }
}
