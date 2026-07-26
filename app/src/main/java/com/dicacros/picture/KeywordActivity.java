package com.dicacros.picture;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class KeywordActivity extends Activity {

    private static final String ADSENSEFARM_URL = "https://adsensefarm.kr/realtime";
    private static final String SIGNAL_URL = "https://www.signal.bz/";
    private static final int REQUEST_NOTIFICATIONS = 3001;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final List<KeywordDatabase.RankedKeyword> latestRankings = new ArrayList<>();
    private final List<String> displayedKeywords = new ArrayList<>();
    private final List<String> currentRankingKeywords = new ArrayList<>();

    private KeywordDatabase database;
    private WebView keywordWeb;
    private EditText manualKeywordInput;
    private EditText relatedOutput;
    private LinearLayout keywordList;
    private TextView summaryText;
    private TextView statusText;
    private ProgressBar progressBar;
    private CheckBox autoSelectCheck;
    private boolean pendingAdsenseExtract;
    private boolean pendingSignalExtract;
    private int rawRealtimeCount;
    private boolean rendering;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        database = new KeywordDatabase(this);
        database.pruneOlderThanDays(10);
        setContentView(createContentView());
        setupKeywordWeb();
        KeywordScheduler.ensureScheduled(this);
        requestNotificationsIfNeeded();
        renderKeywordList();
        refreshRealtimeKeywords();
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        if (keywordWeb != null) {
            keywordWeb.destroy();
        }
        if (database != null) {
            database.close();
        }
        super.onDestroy();
    }

    private View createContentView() {
        ScrollView scrollView = new ScrollView(this);
        scrollView.setFillViewport(true);
        scrollView.setBackgroundColor(UiKit.BACKGROUND);
        LinearLayout root = UiKit.screen(this);
        scrollView.addView(root);

        root.addView(UiKit.backBar(this, "Picture Cleaner · 검색어"));
        root.addView(UiKit.pageTitle(this, "실시간 연관 검색어"));
        root.addView(UiKit.caption(this,
                "저장된 최근 목록을 즉시 표시하고 4개 출처를 새로 갱신합니다."));

        LinearLayout searchCard = UiKit.card(this);
        searchCard.addView(UiKit.sectionTitle(this, "검색어 탐색"));
        autoSelectCheck = optionCheck(
                "지속 검색 가능성이 높은 주제를 자동 선택", AutoConfig.autoKeywordSelection(this));
        autoSelectCheck.setOnCheckedChangeListener((button, checked) -> {
            AutoConfig.setBool(this, "auto_keyword_selection", checked);
            if (checked) {
                runAutoRecommendation();
            } else {
                database.clearAutomaticSelections();
                renderKeywordList();
                setStatus("자동 추천을 껐습니다. 직접 선택한 검색어만 사용합니다.");
            }
        });
        searchCard.addView(autoSelectCheck);
        searchCard.addView(UiKit.caption(this,
                "경기 결과·당첨 번호 같은 일회성 검색어는 제외하고 인물·기업·해외·전망 주제를 우선합니다."));
        manualKeywordInput = input("직접 검색어 입력 후 엔터");
        manualKeywordInput.setSingleLine(true);
        manualKeywordInput.setImeOptions(EditorInfo.IME_ACTION_SEARCH);
        manualKeywordInput.setOnEditorActionListener((view, actionId, event) -> {
            boolean enter = actionId == EditorInfo.IME_ACTION_SEARCH
                    || (event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN);
            if (enter) {
                searchManualKeyword();
                return true;
            }
            return false;
        });
        searchCard.addView(manualKeywordInput);

        LinearLayout refreshRow = row();
        refreshRow.addView(smallButton("순위 새로고침", view -> refreshRealtimeKeywords()));
        refreshRow.addView(accentButton(
                "추천 다시 분석", UiKit.TEAL, view -> runAutoRecommendation()));
        searchCard.addView(refreshRow);

        LinearLayout selectionRow = row();
        selectionRow.addView(smallButton("현재 목록 전체 선택", view -> setDisplayedSelected(true)));
        selectionRow.addView(smallButton("현재 선택 해제", view -> setDisplayedSelected(false)));
        searchCard.addView(selectionRow);
        root.addView(searchCard);

        LinearLayout actionCard = UiKit.card(this);
        actionCard.addView(UiKit.sectionTitle(this, "선택한 주제 활용"));
        Button relatedButton = accentButton(
                "선택 검색어 연관어 조회", UiKit.PRIMARY,
                view -> fetchAllSelectedRelated());
        relatedButton.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(48)));
        actionCard.addView(relatedButton);
        Button copySelected = smallButton(
                "선택 검색어 복사", view -> copySelectedKeywords());
        copySelected.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(48)));
        actionCard.addView(copySelected);
        root.addView(actionCard);

        LinearLayout statusCard = UiKit.card(this);
        statusCard.addView(UiKit.sectionTitle(this, "분석 상태"));
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        UiKit.tintProgress(progressBar, UiKit.TEAL);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(-1, dp(8));
        progressParams.setMargins(0, dp(8), 0, dp(4));
        statusCard.addView(progressBar, progressParams);

        summaryText = UiKit.caption(this, "");
        statusCard.addView(summaryText);
        statusText = UiKit.status(this);
        statusText.setText("저장된 키워드를 불러오는 중입니다.");
        statusCard.addView(statusText);
        root.addView(statusCard);

        LinearLayout keywordCard = UiKit.card(this);
        keywordCard.addView(UiKit.sectionTitle(this, "실시간 검색어와 추천 목록"));
        keywordCard.addView(UiKit.caption(this,
                "체크하면 블로그 자동화의 다음 주제로 사용됩니다."));
        keywordList = new LinearLayout(this);
        keywordList.setOrientation(LinearLayout.VERTICAL);
        keywordCard.addView(keywordList);
        root.addView(keywordCard);

        LinearLayout outputCard = UiKit.card(this);
        outputCard.addView(UiKit.sectionTitle(this, "연관 검색어 결과"));
        Button copyAll = accentButton(
                "전체 내용 복사", UiKit.TEAL, view -> copyRelatedOutput());
        copyAll.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(48)));
        outputCard.addView(copyAll);
        relatedOutput = multiInput(
                "검색어를 선택하거나 직접 입력하면 세 검색엔진의 연관 검색어가 표시됩니다.");
        relatedOutput.setMinLines(8);
        relatedOutput.setKeyListener(null);
        relatedOutput.setTextIsSelectable(true);
        outputCard.addView(relatedOutput);
        root.addView(outputCard);

        return scrollView;
    }

    private void setupKeywordWeb() {
        keywordWeb = new WebView(this);
        WebSettings settings = keywordWeb.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        keywordWeb.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (pendingAdsenseExtract && url != null && url.contains("adsensefarm")) {
                    pendingAdsenseExtract = false;
                    view.postDelayed(() -> extractAdsenseFarmKeywords(), 1200);
                } else if (pendingSignalExtract && url != null
                        && url.contains("signal.bz")) {
                    pendingSignalExtract = false;
                    view.postDelayed(() -> extractSignalKeywords(), 1200);
                }
            }
        });
    }

    private void refreshRealtimeKeywords() {
        progressBar.setProgress(15);
        setStatus("저장된 최근 목록을 표시했습니다. 4개 출처 40개를 갱신합니다.");
        latestRankings.clear();
        rawRealtimeCount = 0;
        pendingSignalExtract = false;
        String current = keywordWeb.getUrl();
        if (current != null && current.contains("adsensefarm")) {
            pendingAdsenseExtract = true;
            keywordWeb.reload();
        } else {
            pendingAdsenseExtract = true;
            keywordWeb.loadUrl(ADSENSEFARM_URL);
        }
    }

    private void extractAdsenseFarmKeywords() {
        keywordWeb.evaluateJavascript(RealtimeKeywordParser.EXTRACT_JS, value -> {
            List<KeywordDatabase.RankedKeyword> rankings = RealtimeKeywordParser.parse(value);
            latestRankings.clear();
            latestRankings.addAll(rankings);
            rawRealtimeCount = rankings.size();
            progressBar.setProgress(35);
            setStatus("애드센스팜 30개를 읽었습니다. 시그널 10개를 확인합니다.");
            pendingSignalExtract = true;
            keywordWeb.loadUrl(SIGNAL_URL);
        });
    }

    private void extractSignalKeywords() {
        keywordWeb.evaluateJavascript(SignalKeywordParser.EXTRACT_JS, value -> {
            List<KeywordDatabase.RankedKeyword> signal = SignalKeywordParser.parse(value);
            latestRankings.addAll(signal);
            rawRealtimeCount += signal.size();
            List<KeywordDatabase.RankedKeyword> filtered =
                    RealtimeKeywordParser.filterContentCandidates(latestRankings);
            latestRankings.clear();
            latestRankings.addAll(filtered);
            database.pruneOlderThanDays(10);
            database.upsertRankings(filtered);
            renderKeywordList();
            int excluded = Math.max(0, rawRealtimeCount - filtered.size());
            setStatus("40개 출처 갱신: 다음 " + countSource(filtered, "다음")
                    + " · 구글 " + countSource(filtered, "구글")
                    + " · 크리에이터 " + countSource(filtered, "네이버")
                    + " · 시그널 " + countSource(filtered, "시그널")
                    + " · 일회성 " + excluded + "개 제외");
            if (autoSelectCheck.isChecked()) {
                runAutoRecommendation();
            } else {
                progressBar.setProgress(100);
            }
        });
    }

    private void runAutoRecommendation() {
        if (!autoSelectCheck.isChecked()) {
            setStatus("롱테일 자동 추천 옵션을 먼저 켜세요.");
            return;
        }
        List<KeywordDatabase.RankedKeyword> rankings =
                new ArrayList<>(latestRankings);
        if (rankings.isEmpty()) {
            setStatus("실시간 순위를 먼저 불러와야 추천할 수 있습니다.");
            return;
        }
        progressBar.setProgress(45);
        setStatus("일회성 검색어를 제외하고 연관 질문의 깊이를 분석하고 있습니다.");
        executor.execute(() -> {
            KeywordAutomationEngine.Result result =
                    KeywordAutomationEngine.enrichAndRecommend(
                            database, rankings, 8, 12);
            runOnUiThread(() -> {
                progressBar.setProgress(100);
                renderKeywordList();
                setStatus("후보 " + result.seeds + "개의 연관어 "
                        + result.related + "개를 분석해 롱테일 "
                        + result.selected + "개를 자동 선택했습니다.");
            });
        });
    }

    private int countSource(List<KeywordDatabase.RankedKeyword> rankings, String source) {
        int count = 0;
        for (KeywordDatabase.RankedKeyword ranking : rankings) {
            if (source.equals(ranking.source)) {
                count++;
            }
        }
        return count;
    }

    private void renderKeywordList() {
        if (keywordList == null) {
            return;
        }
        rendering = true;
        keywordList.removeAllViews();
        displayedKeywords.clear();
        currentRankingKeywords.clear();
        List<KeywordDatabase.KeywordRecord> stored = database.loadKeywords(500);
        Set<String> selected = new LinkedHashSet<>();
        for (KeywordDatabase.KeywordRecord record : stored) {
            if ((record.selected || record.autoSelected) && !record.excluded
                    && !KeywordInterestScorer.isEphemeral(record.keyword)) {
                selected.add(record.keyword);
            }
        }

        Set<String> latestValues = new LinkedHashSet<>();
        if (!latestRankings.isEmpty()) {
            addSourceSection("다음 실시간 1~10위", "다음", selected, latestValues);
            addSourceSection("구글 실시간 1~10위", "구글", selected, latestValues);
            addSourceSection("크리에이터 어드바이저 1~10위", "네이버",
                    selected, latestValues);
            addSourceSection("시그널 실시간 1~10위", "시그널", selected, latestValues);
        }

        List<KeywordDatabase.KeywordRecord> history = new ArrayList<>();
        for (KeywordDatabase.KeywordRecord record : stored) {
            if (!latestValues.contains(record.keyword)
                    && !KeywordInterestScorer.isEphemeral(record.keyword)) {
                history.add(record);
            }
        }
        if (!history.isEmpty()) {
            keywordList.addView(sectionLabel("DB 누적 검색어"));
            addStoredRows(history, selected);
        } else if (latestRankings.isEmpty()) {
            keywordList.addView(smallLabel("아직 저장된 검색어가 없습니다."));
        }
        rendering = false;
        updateSummary();
    }

    private void addSourceSection(String title, String source, Set<String> selected,
                                  Set<String> latestValues) {
        keywordList.addView(sectionLabel(title));
        List<KeywordDatabase.RankedKeyword> sourceItems = new ArrayList<>();
        for (KeywordDatabase.RankedKeyword ranking : latestRankings) {
            if (source.equals(ranking.source)) {
                sourceItems.add(ranking);
                latestValues.add(ranking.keyword);
                currentRankingKeywords.add(ranking.keyword);
            }
        }
        for (int index = 0; index < sourceItems.size(); index++) {
            KeywordDatabase.RankedKeyword ranking = sourceItems.get(index);
            CheckBox box = keywordCheck(
                    ranking.rank + "위 " + ranking.keyword,
                    ranking.keyword,
                    selected.contains(ranking.keyword));
            keywordList.addView(box);
        }
    }

    private void addStoredRows(List<KeywordDatabase.KeywordRecord> records,
                               Set<String> selected) {
        for (int index = 0; index < records.size(); index++) {
            KeywordDatabase.KeywordRecord record = records.get(index);
            String source = record.sources.isEmpty() ? "" : record.sources + " · ";
            String recommendation = record.autoSelected && !record.selected
                    ? "자동추천 " + record.interestScore + "점 · " : "";
            CheckBox box = keywordCheck(
                    recommendation + source + record.keyword,
                    record.keyword,
                    selected.contains(record.keyword));
            keywordList.addView(box);
        }
    }

    private CheckBox keywordCheck(String text, String keyword, boolean checked) {
        CheckBox box = new CheckBox(this);
        box.setText(text);
        box.setTag(keyword);
        box.setChecked(checked);
        box.setTextSize(14);
        box.setTextColor(UiKit.INK);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(dp(10), dp(7), dp(10), dp(7));
        box.setBackground(UiKit.rounded(UiKit.SURFACE_SOFT, 10, this));
        UiKit.styleCheck(box);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(4), 0, dp(4));
        box.setLayoutParams(params);
        displayedKeywords.add(keyword);
        box.setOnCheckedChangeListener((button, isChecked) -> {
            if (rendering) {
                return;
            }
            database.setSelected(keyword, isChecked);
            renderKeywordList();
            if (isChecked) {
                fetchRelated(java.util.Collections.singletonList(keyword));
            }
        });
        return box;
    }

    private void setDisplayedSelected(boolean selected) {
        List<String> source = currentRankingKeywords.isEmpty()
                ? displayedKeywords
                : currentRankingKeywords;
        List<String> unique = new ArrayList<>(new LinkedHashSet<>(source));
        database.setAllSelected(unique, selected);
        renderKeywordList();
        setStatus(unique.size() + "개 검색어 선택 상태를 변경했습니다.");
    }

    private void searchManualKeyword() {
        String keyword = KeywordDatabase.normalizeKeyword(
                manualKeywordInput.getText().toString());
        if (!KeywordDatabase.isUsableKeyword(keyword)) {
            setStatus("두 글자 이상의 검색어를 입력하세요.");
            return;
        }
        database.addManualKeyword(keyword);
        renderKeywordList();
        fetchRelated(java.util.Collections.singletonList(keyword));
    }

    private void fetchAllSelectedRelated() {
        List<String> seeds = new ArrayList<>();
        for (KeywordDatabase.KeywordRecord record : database.loadSelectedKeywords()) {
            if (!KeywordInterestScorer.isEphemeral(record.keyword)) {
                seeds.add(record.keyword);
            }
        }
        if (seeds.isEmpty()) {
            setStatus("먼저 검색어를 하나 이상 선택하세요.");
            return;
        }
        fetchRelated(seeds);
    }

    private void fetchRelated(List<String> rawSeeds) {
        List<String> seeds = new ArrayList<>(new LinkedHashSet<>(rawSeeds));
        progressBar.setProgress(45);
        setStatus("선택한 " + seeds.size() + "개 검색어를 세 검색엔진에서 조회합니다.");
        executor.execute(() -> {
            StringBuilder output = new StringBuilder("선택 검색어: ")
                    .append(BlogGenerator.join(seeds, ", "))
                    .append("\n\n");
            Set<String> all = new LinkedHashSet<>();
            int errorCount = 0;
            for (RelatedKeywordFetcher.Result result
                    : RelatedKeywordFetcher.fetchAll(seeds, 4)) {
                database.saveRelated(result);
                for (String value : result.all()) {
                    String keyword = KeywordDatabase.normalizeKeyword(value);
                    if (KeywordDatabase.isUsableKeyword(keyword)
                            && !KeywordInterestScorer.isEphemeral(keyword)) {
                        all.add(keyword);
                    }
                }
                errorCount += result.errors.size();
            }
            for (String keyword : all) {
                output.append(keyword).append('\n');
            }
            if (autoSelectCheck.isChecked()) {
                database.refreshAutomaticSelections(12);
            }
            int finalErrorCount = errorCount;
            runOnUiThread(() -> {
                relatedOutput.setText(output.toString().trim());
                progressBar.setProgress(100);
                updateSummary();
                String message = "연관 검색어 " + all.size() + "개를 DB에 저장했습니다.";
                if (finalErrorCount > 0) {
                    message += " 일부 조회 " + finalErrorCount + "건은 실패했습니다.";
                }
                setStatus(message);
            });
        });
    }

    private void copySelectedKeywords() {
        List<String> values = new ArrayList<>();
        for (KeywordDatabase.KeywordRecord record : database.loadSelectedKeywords()) {
            if (!KeywordInterestScorer.isEphemeral(record.keyword)) {
                values.add(record.keyword);
            }
        }
        if (values.isEmpty()) {
            setStatus("복사할 선택 검색어가 없습니다.");
            return;
        }
        copy(BlogGenerator.join(values, "\n"), "selected keywords");
        setStatus("선택 검색어 " + values.size() + "개를 복사했습니다.");
    }

    private void copyRelatedOutput() {
        String value = relatedOutput.getText().toString().trim();
        if (value.isEmpty()) {
            setStatus("복사할 연관 검색어 결과가 없습니다.");
            return;
        }
        copy(value, "related keyword result");
        setStatus("선택 검색어와 전체 연관 검색어를 복사했습니다.");
    }

    private void copy(String text, String label) {
        ClipboardManager clipboard =
                (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard != null) {
            clipboard.setPrimaryClip(ClipData.newPlainText(label, text));
        }
    }

    private void updateSummary() {
        summaryText.setText("DB " + database.keywordCount()
                + "개 · 선택 " + database.selectedCount()
                + "개(자동 " + database.automaticSelectedCount() + "개)"
                + " · 연관어 " + database.relatedCount() + "개");
    }

    private void requestNotificationsIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    REQUEST_NOTIFICATIONS);
        }
    }

    private TextView title(String text) {
        return UiKit.pageTitle(this, text);
    }

    private TextView label(String text) {
        return UiKit.sectionTitle(this, text);
    }

    private TextView sectionLabel(String text) {
        TextView view = UiKit.sectionTitle(this, text);
        view.setTextColor(UiKit.NAVY);
        view.setPadding(0, dp(16), 0, dp(5));
        return view;
    }

    private TextView smallLabel(String text) {
        return UiKit.body(this, text);
    }

    private EditText input(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        UiKit.styleInput(input, false);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(48));
        params.setMargins(0, dp(8), 0, dp(6));
        input.setLayoutParams(params);
        return input;
    }

    private EditText multiInput(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setGravity(Gravity.TOP | Gravity.START);
        UiKit.styleInput(input, true);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(6), 0, dp(6));
        input.setLayoutParams(params);
        return input;
    }

    private CheckBox optionCheck(String text, boolean checked) {
        CheckBox box = new CheckBox(this);
        box.setText(text);
        box.setChecked(checked);
        UiKit.styleCheck(box);
        return box;
    }

    private LinearLayout row() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(3), 0, dp(3));
        return row;
    }

    private Button smallButton(String text, View.OnClickListener listener) {
        Button button = UiKit.secondaryButton(this, text);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1f);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        button.setLayoutParams(params);
        return button;
    }

    private Button accentButton(
            String text, int color, View.OnClickListener listener) {
        Button button = UiKit.primaryButton(this, text, color);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1f);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        button.setLayoutParams(params);
        return button;
    }

    private void setStatus(String message) {
        statusText.setText(message);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
