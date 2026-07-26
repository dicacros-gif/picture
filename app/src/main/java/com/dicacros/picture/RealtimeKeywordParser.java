package com.dicacros.picture;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.util.ArrayList;
import java.util.List;

final class RealtimeKeywordParser {

    static final String EXTRACT_JS =
            "(function(){try{"
                    + "var defs=["
                    + "{source:'다음',names:['다음 실시간 검색어']},"
                    + "{source:'구글',names:['구글 실시간 검색어']},"
                    + "{source:'네이버',names:['크리에이터 어드바이저 검색어','네이버 실시간 검색어']}"
                    + "];var out=[];var cards=document.querySelectorAll('.item');"
                    + "for(var d=0;d<defs.length;d++){var card=null;"
                    + "for(var i=0;i<cards.length&&!card;i++){"
                    + "var h=cards[i].querySelector('h2');var title=(h?h.innerText:'').trim();"
                    + "if(defs[d].names.indexOf(title)>=0)card=cards[i];}"
                    + "if(!card)continue;var links=card.querySelectorAll('.kwds .keyword a');"
                    + "for(var k=0;k<links.length&&k<10;k++){"
                    + "var text=(links[k].innerText||links[k].textContent||'').replace(/\\s+/g,' ').trim();"
                    + "if(text)out.push({keyword:text,source:defs[d].source,rank:k+1});}}"
                    + "return JSON.stringify(out);}catch(e){return JSON.stringify([]);}})();";

    private RealtimeKeywordParser() {
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
            for (int index = 0; index < array.length(); index++) {
                JSONObject item = array.optJSONObject(index);
                if (item == null) {
                    continue;
                }
                String keyword = KeywordDatabase.normalizeKeyword(
                        item.optString("keyword", ""));
                String source = item.optString("source", "");
                int rank = item.optInt("rank", index + 1);
                if (KeywordDatabase.isUsableKeyword(keyword)
                        && ("네이버".equals(source)
                        || "다음".equals(source)
                        || "구글".equals(source))) {
                    result.add(new KeywordDatabase.RankedKeyword(keyword, source, rank));
                }
            }
        } catch (Exception ignored) {
        }
        return result;
    }
}
