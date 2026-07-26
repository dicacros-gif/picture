package com.dicacros.picture;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.util.ArrayList;
import java.util.List;

final class SignalKeywordParser {

    static final String EXTRACT_JS =
            "(function(){try{var out=[];"
                    + "var nodes=document.querySelectorAll('.realtime-rank .rank-text');"
                    + "if(!nodes.length)nodes=document.querySelectorAll('.rank-text');"
                    + "for(var i=0;i<nodes.length&&i<10;i++){"
                    + "var text=(nodes[i].innerText||nodes[i].textContent||'')"
                    + ".replace(/\\s+/g,' ').trim();"
                    + "if(text)out.push({keyword:text,source:'시그널',rank:i+1});}"
                    + "return JSON.stringify(out);"
                    + "}catch(e){return JSON.stringify([]);}})();";

    private SignalKeywordParser() {
    }

    static List<KeywordDatabase.RankedKeyword> parse(String rawValue) {
        List<KeywordDatabase.RankedKeyword> result = new ArrayList<>();
        if (rawValue == null || rawValue.isEmpty() || "null".equals(rawValue)) {
            return result;
        }
        try {
            Object first = new JSONTokener(rawValue).nextValue();
            String inner = first instanceof String ? (String) first : rawValue;
            JSONArray array = new JSONArray(inner);
            for (int index = 0; index < array.length() && index < 10; index++) {
                JSONObject item = array.optJSONObject(index);
                if (item == null) {
                    continue;
                }
                String keyword = KeywordDatabase.normalizeKeyword(
                        item.optString("keyword", ""));
                if (KeywordDatabase.isUsableKeyword(keyword)) {
                    result.add(new KeywordDatabase.RankedKeyword(
                            keyword, "시그널", item.optInt("rank", index + 1)));
                }
            }
        } catch (Exception ignored) {
        }
        return result;
    }
}
