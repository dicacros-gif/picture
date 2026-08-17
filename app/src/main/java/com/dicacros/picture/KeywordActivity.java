package com.dicacros.picture;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class KeywordActivity extends Activity {

    private static final int REQUEST_NOTIFICATIONS = 3001;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final List<KeywordDatabase.RankedKeyword> latestRankings = new ArrayList<>();
    private final List<KeywordDatabase.SnapshotInfo> snapshots = new ArrayList<>();
    private final List<String> snapshotDateKeys = new ArrayList<>();
    private final Map<String, List<KeywordDatabase.SnapshotInfo>> snapshotsByDate =
            new LinkedHashMap<>();
    private final List<KeywordDatabase.SnapshotInfo> timeOptions = new ArrayList<>();

    private KeywordDatabase database;
    private ScrollView rootScroll;
    private EditText manualKeywordInput;
    private EditText relatedOutput;
    private LinearLayout keywordList;
    private TextView summaryText;
    private TextView statusText;
    private TextView selectedKeywordText;
    private View relatedResultCard;
    private ProgressBar progressBar;
    private CheckBox autoSelectCheck;
    private Spinner dateSpinner;
    private Spinner timeSpinner;
    private ArrayAdapter<String> dateAdapter;
    private ArrayAdapter<String> timeAdapter;
    private KeywordDatabase.SnapshotInfo currentSnapshot;
    private BroadcastReceiver collectionReceiver;
    private boolean collectionReceiverRegistered;
    private boolean updatingSnapshotSelectors;
    private boolean rendering;
    private int relatedRequestToken;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        database = new KeywordDatabase(this);
        database.pruneOlderThanDays(7);
        database.retainSingleSelection();
        setContentView(createContentView());
        registerCollectionReceiver();
        requestNotificationsIfNeeded();
        reloadSnapshotSelectors(-1L);
        if (KeywordCollectorService.isCollecting()) {
            progressBar.setProgress(35);
            setStatus("앱 시작 새로고침이 진행 중입니다.");
        }
    }

    @Override
    protected void onDestroy() {
        if (collectionReceiverRegistered) {
            try {
                unregisterReceiver(collectionReceiver);
            } catch (Throwable ignored) {
            }
            collectionReceiverRegistered = false;
        }
        executor.shutdownNow();
        if (database != null) {
            database.close();
        }
        super.onDestroy();
    }

    private View createContentView() {
        rootScroll = new ScrollView(this);
        rootScroll.setFillViewport(true);
        rootScroll.setVerticalScrollBarEnabled(true);
        rootScroll.setScrollbarFadingEnabled(false);
        rootScroll.setBackgroundColor(UiKit.BACKGROUND);
        LinearLayout root = UiKit.screen(this);
        rootScroll.addView(root);

        root.addView(UiKit.backBar(this, "Picture Cleaner · 검색어"));
        root.addView(UiKit.pageTitle(this, "실시간 연관 검색어"));
        root.addView(UiKit.caption(this,
                "앱 실행 또는 새로고침 버튼을 누를 때만 다음·Google·애드센스팜·시그널을 확인합니다."));

        LinearLayout searchCard = UiKit.card(this);
        searchCard.addView(UiKit.sectionTitle(this, "검색어 탐색"));
        autoSelectCheck = optionCheck(
                "지속 검색 가능성이 높은 주제 1개 자동 추천",
                AutoConfig.autoKeywordSelection(this));
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
        refreshRow.addView(smallButton(
                "4출처 새로고침", view -> refreshRealtimeKeywords()));
        refreshRow.addView(accentButton(
                "추천 다시 분석", UiKit.TEAL, view -> runAutoRecommendation()));
        searchCard.addView(refreshRow);
        root.addView(searchCard);

        LinearLayout historyCard = UiKit.card(this);
        historyCard.addView(UiKit.sectionTitle(this, "저장 기록"));
        historyCard.addView(UiKit.caption(this,
                "최근 7일간 저장된 수집 날짜와 시간을 선택하면 당시 키워드를 끝까지 스크롤해 볼 수 있습니다."));
        historyCard.addView(UiKit.body(this, "날짜"));
        dateSpinner = createSpinner();
        dateAdapter = createSpinnerAdapter();
        dateSpinner.setAdapter(dateAdapter);
        historyCard.addView(dateSpinner);
        historyCard.addView(UiKit.body(this, "시간"));
        timeSpinner = createSpinner();
        timeAdapter = createSpinnerAdapter();
        timeSpinner.setAdapter(timeAdapter);
        historyCard.addView(timeSpinner);
        setupSnapshotListeners();
        root.addView(historyCard);

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
        keywordCard.addView(UiKit.sectionTitle(this, "선택한 시점의 검색어"));
        keywordCard.addView(UiKit.caption(this,
                "한 번에 하나만 체크할 수 있으며 선택하면 연관 검색어를 바로 조회합니다."));
        keywordList = new LinearLayout(this);
        keywordList.setOrientation(LinearLayout.VERTICAL);
        keywordCard.addView(keywordList);
        root.addView(keywordCard);

        LinearLayout outputCard = UiKit.card(this);
        outputCard.addView(UiKit.sectionTitle(this, "연관 검색어 결과"));
        selectedKeywordText = UiKit.body(this, "선택 검색어: -");
        outputCard.addView(selectedKeywordText);
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
        relatedResultCard = outputCard;

        return rootScroll;
    }

    private void setupSnapshotListeners() {
        dateSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(
                    AdapterView<?> parent, View view, int position, long id) {
                if (updatingSnapshotSelectors
                        || position < 0 || position >= snapshotDateKeys.size()) {
                    return;
                }
                updatingSnapshotSelectors = true;
                populateTimeOptions(snapshotDateKeys.get(position), -1L);
                updatingSnapshotSelectors = false;
                showCurrentTimeOption();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
        timeSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(
                    AdapterView<?> parent, View view, int position, long id) {
                if (!updatingSnapshotSelectors
                        && position >= 0 && position < timeOptions.size()) {
                    showSnapshot(timeOptions.get(position));
                }
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    private void registerCollectionReceiver() {
        collectionReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (intent == null
                        || !KeywordCollectorService.ACTION_COLLECTION_FINISHED
                        .equals(intent.getAction())) {
                    return;
                }
                long snapshotId = intent.getLongExtra(
                        KeywordCollectorService.EXTRA_SNAPSHOT_ID, -1L);
                boolean success = intent.getBooleanExtra(
                        KeywordCollectorService.EXTRA_SUCCESS, false);
                String message = intent.getStringExtra(
                        KeywordCollectorService.EXTRA_MESSAGE);
                database.pruneOlderThanDays(7);
                reloadSnapshotSelectors(snapshotId);
                progressBar.setProgress(success ? 100 : 0);
                setStatus(message == null ? "새로고침을 마쳤습니다." : message);
            }
        };
        IntentFilter filter =
                new IntentFilter(KeywordCollectorService.ACTION_COLLECTION_FINISHED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(collectionReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(collectionReceiver, filter);
        }
        collectionReceiverRegistered = true;
    }

    private void refreshRealtimeKeywords() {
        if (KeywordCollectorService.isCollecting()) {
            progressBar.setProgress(35);
            setStatus("이미 다음·Google Trends를 새로고침하고 있습니다.");
            return;
        }
        progressBar.setProgress(15);
        setStatus("다음·Google 직접 수집 후 애드센스팜·시그널을 순서대로 확인합니다.");
        KeywordScheduler.collectNow(this);
    }

    private void reloadSnapshotSelectors(long preferredSnapshotId) {
        long fallbackId = currentSnapshot == null ? -1L : currentSnapshot.id;
        snapshots.clear();
        snapshots.addAll(database.loadSnapshots(300));
        snapshotsByDate.clear();
        snapshotDateKeys.clear();
        for (KeywordDatabase.SnapshotInfo snapshot : snapshots) {
            String key = formatDateKey(snapshot.capturedAt);
            List<KeywordDatabase.SnapshotInfo> group = snapshotsByDate.get(key);
            if (group == null) {
                group = new ArrayList<>();
                snapshotsByDate.put(key, group);
                snapshotDateKeys.add(key);
            }
            group.add(snapshot);
        }

        updatingSnapshotSelectors = true;
        dateAdapter.clear();
        if (snapshots.isEmpty()) {
            dateAdapter.add("저장 기록 없음");
            timeAdapter.clear();
            timeAdapter.add("-");
            currentSnapshot = null;
            latestRankings.clear();
            dateSpinner.setEnabled(false);
            timeSpinner.setEnabled(false);
            updatingSnapshotSelectors = false;
            renderKeywordList();
            if (!KeywordCollectorService.isCollecting()) {
                setStatus("저장 기록이 없습니다. 새로고침 버튼을 눌러 수집하세요.");
            }
            return;
        }

        dateSpinner.setEnabled(true);
        timeSpinner.setEnabled(true);
        long targetId = preferredSnapshotId > 0L ? preferredSnapshotId : fallbackId;
        if (targetId <= 0L) {
            targetId = snapshots.get(0).id;
        }
        int targetDateIndex = 0;
        for (int index = 0; index < snapshotDateKeys.size(); index++) {
            String key = snapshotDateKeys.get(index);
            List<KeywordDatabase.SnapshotInfo> group = snapshotsByDate.get(key);
            dateAdapter.add(formatDateLabel(group));
            if (containsSnapshot(group, targetId)) {
                targetDateIndex = index;
            }
        }
        dateSpinner.setSelection(targetDateIndex, false);
        populateTimeOptions(snapshotDateKeys.get(targetDateIndex), targetId);
        updatingSnapshotSelectors = false;
        showCurrentTimeOption();
    }

    private void populateTimeOptions(String dateKey, long preferredSnapshotId) {
        timeOptions.clear();
        List<KeywordDatabase.SnapshotInfo> group = snapshotsByDate.get(dateKey);
        if (group != null) {
            timeOptions.addAll(group);
        }
        timeAdapter.clear();
        int targetIndex = 0;
        for (int index = 0; index < timeOptions.size(); index++) {
            KeywordDatabase.SnapshotInfo snapshot = timeOptions.get(index);
            timeAdapter.add(formatTimeLabel(snapshot));
            if (snapshot.id == preferredSnapshotId) {
                targetIndex = index;
            }
        }
        if (timeOptions.isEmpty()) {
            timeAdapter.add("-");
        } else {
            timeSpinner.setSelection(targetIndex, false);
        }
    }

    private void showCurrentTimeOption() {
        int position = timeSpinner.getSelectedItemPosition();
        if (position < 0 || position >= timeOptions.size()) {
            position = 0;
        }
        if (!timeOptions.isEmpty()) {
            showSnapshot(timeOptions.get(position));
        }
    }

    private void showSnapshot(KeywordDatabase.SnapshotInfo snapshot) {
        currentSnapshot = snapshot;
        latestRankings.clear();
        latestRankings.addAll(database.loadSnapshot(snapshot.id));
        renderKeywordList();
        setStatus(formatDateTime(snapshot.capturedAt)
                + " 저장 기록 " + snapshot.itemCount + "개를 표시합니다.");
    }

    private void runAutoRecommendation() {
        if (!autoSelectCheck.isChecked()) {
            setStatus("롱테일 자동 추천 옵션을 먼저 켜세요.");
            return;
        }
        List<KeywordDatabase.RankedKeyword> rankings =
                new ArrayList<>(latestRankings);
        if (rankings.isEmpty()) {
            setStatus("표시된 저장 기록이 없어 추천할 수 없습니다.");
            return;
        }
        progressBar.setProgress(45);
        setStatus("일회성 검색어를 제외하고 연관 질문의 깊이를 분석하고 있습니다.");
        executor.execute(() -> {
            KeywordAutomationEngine.Result result =
                    KeywordAutomationEngine.enrichAndRecommend(
                            database, rankings, 8, 1);
            runOnUiThread(() -> {
                progressBar.setProgress(100);
                renderKeywordList();
                setStatus("후보 " + result.seeds + "개의 연관어 "
                        + result.related + "개를 분석해 롱테일 "
                        + result.selected + "개를 자동 선택했습니다.");
            });
        });
    }

    private void renderKeywordList() {
        if (keywordList == null) {
            return;
        }
        rendering = true;
        keywordList.removeAllViews();
        List<KeywordDatabase.KeywordRecord> stored = database.loadKeywords(500);
        Set<String> selected = new LinkedHashSet<>();
        for (KeywordDatabase.KeywordRecord record : stored) {
            if ((record.selected || record.autoSelected) && !record.excluded
                    && !KeywordInterestScorer.isEphemeral(record.keyword)) {
                selected.add(record.keyword);
            }
        }

        Set<String> displayed = new LinkedHashSet<>();
        addSourceSection("다음", "다음", selected, displayed);
        addSourceSection("구글", "구글", selected, displayed);
        addSourceSection("크리에이터 어드바이저", "네이버", selected, displayed);
        addSourceSection("네이버 시그널", "시그널", selected, displayed);
        Set<String> extraSources = new LinkedHashSet<>();
        for (KeywordDatabase.RankedKeyword ranking : latestRankings) {
            if (!"다음".equals(ranking.source)
                    && !"구글".equals(ranking.source)
                    && !"네이버".equals(ranking.source)
                    && !"시그널".equals(ranking.source)) {
                extraSources.add(ranking.source);
            }
        }
        for (String source : extraSources) {
            addSourceSection("기존 " + source + " 기록", source, selected, displayed);
        }
        addSelectedOutsideSnapshot(stored, selected, displayed);
        if (keywordList.getChildCount() == 0) {
            keywordList.addView(smallLabel(
                    "표시할 저장 기록이 없습니다. 새로고침 버튼을 눌러 주세요."));
        }
        rendering = false;
        updateSummary();
    }

    private void addSourceSection(
            String title, String source, Set<String> selected, Set<String> displayed) {
        List<KeywordDatabase.RankedKeyword> sourceItems = new ArrayList<>();
        for (KeywordDatabase.RankedKeyword ranking : latestRankings) {
            String key = ranking.keyword.toLowerCase(Locale.ROOT);
            if (source.equals(ranking.source)
                    && !KeywordInterestScorer.isEphemeral(ranking.keyword)
                    && !displayed.contains(key)) {
                sourceItems.add(ranking);
                displayed.add(key);
            }
        }
        if (sourceItems.isEmpty()) {
            return;
        }
        keywordList.addView(sectionLabel(title));
        for (KeywordDatabase.RankedKeyword ranking : sourceItems) {
            keywordList.addView(keywordCheck(
                    ranking.rank + "위 " + ranking.keyword,
                    ranking.keyword,
                    selected.contains(ranking.keyword)));
        }
    }

    private void addSelectedOutsideSnapshot(
            List<KeywordDatabase.KeywordRecord> stored, Set<String> selected,
            Set<String> displayed) {
        List<KeywordDatabase.KeywordRecord> outside = new ArrayList<>();
        for (KeywordDatabase.KeywordRecord record : stored) {
            String key = record.keyword.toLowerCase(Locale.ROOT);
            if (selected.contains(record.keyword) && !displayed.contains(key)) {
                outside.add(record);
                displayed.add(key);
            }
        }
        if (outside.isEmpty()) {
            return;
        }
        keywordList.addView(sectionLabel("현재 선택"));
        for (KeywordDatabase.KeywordRecord record : outside) {
            keywordList.addView(keywordCheck(
                    record.keyword, record.keyword, true));
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
        box.setOnCheckedChangeListener((button, isChecked) -> {
            if (rendering) {
                return;
            }
            database.setSelected(keyword, isChecked);
            updateSummary();
            if (isChecked) {
                uncheckOtherKeywordBoxes(box);
                selectedKeywordText.setText("선택 검색어: " + keyword);
                relatedOutput.setText("");
                fetchRelated(java.util.Collections.singletonList(keyword));
            } else {
                selectedKeywordText.setText("선택 검색어: -");
                relatedOutput.setText("");
                setStatus(keyword + " 선택을 해제했습니다.");
            }
        });
        return box;
    }

    private void uncheckOtherKeywordBoxes(CheckBox selectedBox) {
        if (keywordList == null) {
            return;
        }
        rendering = true;
        for (int index = 0; index < keywordList.getChildCount(); index++) {
            View child = keywordList.getChildAt(index);
            if (child instanceof CheckBox && child != selectedBox) {
                ((CheckBox) child).setChecked(false);
            }
        }
        rendering = false;
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
            setStatus("먼저 검색어를 하나 선택하세요.");
            return;
        }
        fetchRelated(seeds);
    }

    private void fetchRelated(List<String> rawSeeds) {
        List<String> seeds = new ArrayList<>(new LinkedHashSet<>(rawSeeds));
        int requestToken = ++relatedRequestToken;
        selectedKeywordText.setText(
                "선택 검색어: " + BlogGenerator.join(seeds, ", "));
        relatedOutput.setText("");
        scrollToRelatedResults();
        progressBar.setProgress(45);
        setStatus("선택한 검색어를 네이버·다음·구글에서 조회합니다.");
        executor.execute(() -> {
            StringBuilder output = new StringBuilder();
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
                database.refreshAutomaticSelections(1);
            }
            int finalErrorCount = errorCount;
            runOnUiThread(() -> {
                if (requestToken != relatedRequestToken) {
                    return;
                }
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
        setStatus("선택 검색어 제목을 제외하고 연관 검색어 내용만 복사했습니다.");
    }

    private void copy(String text, String label) {
        ClipboardManager clipboard =
                (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard != null) {
            clipboard.setPrimaryClip(ClipData.newPlainText(label, text));
        }
    }

    private void scrollToRelatedResults() {
        if (rootScroll == null || relatedResultCard == null) {
            return;
        }
        rootScroll.post(() ->
                rootScroll.smoothScrollTo(0, relatedResultCard.getTop()));
    }

    private void updateSummary() {
        int displayed = currentSnapshot == null ? 0 : currentSnapshot.itemCount;
        summaryText.setText("저장 시점 " + database.snapshotCount()
                + "개 · 현재 " + displayed + "개 · 선택 "
                + database.selectedCount() + "개(자동 "
                + database.automaticSelectedCount() + "개) · 연관어 "
                + database.relatedCount() + "개");
    }

    private boolean containsSnapshot(
            List<KeywordDatabase.SnapshotInfo> group, long snapshotId) {
        if (group == null) {
            return false;
        }
        for (KeywordDatabase.SnapshotInfo snapshot : group) {
            if (snapshot.id == snapshotId) {
                return true;
            }
        }
        return false;
    }

    private String formatDateKey(long timestamp) {
        return new SimpleDateFormat("yyyy-MM-dd", Locale.KOREA)
                .format(new Date(timestamp));
    }

    private String formatDateLabel(List<KeywordDatabase.SnapshotInfo> group) {
        if (group == null || group.isEmpty()) {
            return "-";
        }
        return new SimpleDateFormat("yyyy년 M월 d일", Locale.KOREA)
                .format(new Date(group.get(0).capturedAt))
                + " · " + group.size() + "회";
    }

    private String formatTimeLabel(KeywordDatabase.SnapshotInfo snapshot) {
        return new SimpleDateFormat("HH:mm:ss", Locale.KOREA)
                .format(new Date(snapshot.capturedAt))
                + " · " + snapshot.itemCount + "개";
    }

    private String formatDateTime(long timestamp) {
        return new SimpleDateFormat("yyyy년 M월 d일 HH:mm:ss", Locale.KOREA)
                .format(new Date(timestamp));
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

    private ArrayAdapter<String> createSpinnerAdapter() {
        ArrayAdapter<String> adapter = new ArrayAdapter<>(
                this, android.R.layout.simple_spinner_item, new ArrayList<>());
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        return adapter;
    }

    private Spinner createSpinner() {
        Spinner spinner = new Spinner(this);
        spinner.setPadding(dp(12), 0, dp(12), 0);
        spinner.setBackground(UiKit.rounded(UiKit.SURFACE_SOFT, 12, this));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(48));
        params.setMargins(0, dp(5), 0, dp(10));
        spinner.setLayoutParams(params);
        return spinner;
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
