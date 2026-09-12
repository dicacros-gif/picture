import copy
import unittest
from blog_visual_style import (QUOTE_LAYOUTS, HEADING_BACKGROUNDS, choose_visual_style, line_style_runs)
from naver_automation import NaverAutomation
from test_naver_publish import document


class VisualStyleTests(unittest.TestCase):
    def setUp(self):
        self.sections=[f'──────────────\n❝ 기준 {i}을 정하려면?\n\n기부단체와 세액공제를 확인해요.\n\n{i}번의 아주 중요한 판단 기준은 공식 자료에서 확인해야 해요.' for i in range(8)]
        self.phrase=self.sections[0].split('\n')[-1]
        self.options=choose_visual_style(self.sections,['기부단체와 세액공제를 확인해요.'],[self.phrase])
        self.ids=['a','b','c','d','e','f']
        self.positions=[0,1,3,4,6,7]

    def arrange(self):
        return NaverAutomation._arrange_article_document(document(image_ids=self.ids),self.sections,self.ids,self.positions,
                  bold_terms=['기부단체','세액공제'],bold_style={'bold':True},visual_style=self.options)

    def verify(self,data):
        return NaverAutomation._verify_article_document(data,self.sections,self.ids,self.positions,
                  bold_terms=['기부단체','세액공제'],bold_style={'bold':True},visual_style=self.options)

    def test_six_native_styles_shuffled_and_options_do_not_change_on_rearrangement(self):
        self.assertEqual(set(self.options['quote_layouts'][:6]),set(QUOTE_LAYOUTS))
        before=copy.deepcopy(self.options)
        data=self.arrange()
        self.assertTrue(self.verify(data))
        repeated=NaverAutomation._arrange_article_document(data,self.sections,self.ids,self.positions,
                  bold_terms=['기부단체','세액공제'],bold_style={'bold':True},visual_style=self.options)
        self.assertTrue(self.verify(repeated))
        self.assertEqual(self.options,before)
        quotes=[c for c in repeated['document']['components'] if c['@ctype']=='quotation']
        self.assertEqual(len(quotes),8)
        self.assertEqual([c['layout'] for c in quotes],self.options['quote_layouts'])

    def test_native_quote_layout_tamper_and_body_move_fail_verification(self):
        data=self.arrange()
        quote=next(c for c in data['document']['components'] if c['@ctype']=='quotation')
        quote['layout']='unknown'
        self.assertFalse(self.verify(data))
        data=self.arrange()
        data['document']['components'][1:3]=reversed(data['document']['components'][1:3])
        self.assertFalse(self.verify(data))

    def test_no_character_underline_and_no_background_in_quote(self):
        data=self.arrange()
        for c in data['document']['components']:
            for row in c.get('value') or []:
                for node in row['nodes']:
                    self.assertFalse(node['style'].get('underline'))
                    if c['@ctype']=='quotation':
                        self.assertNotIn('backgroundColor',node['style'])
        quote=next(c for c in data['document']['components'] if c['@ctype']=='quotation')
        quote['value'][0]['nodes'][0]['style']['underline']=True
        self.assertFalse(self.verify(data))

    def test_important_content_bold_and_keywords_different_colors(self):
        line='기부단체와 세액공제를 확인해요.'
        runs=line_style_runs(line,['기부단체','세액공제'],0,self.options)
        self.assertEqual(''.join(value for value,_ in runs),line)
        self.assertTrue(all(style.get('bold') for value,style in runs))
        colors=[style['fontColor'] for _,style in runs if 'fontColor' in style]
        self.assertEqual(len(set(colors)),2)
        self.assertTrue(all('backgroundColor' not in style for _,style in runs))

    def test_only_very_important_sentence_receives_pale_background(self):
        runs=line_style_runs(self.phrase,['공식 자료'],0,self.options)
        color=self.options['highlight_phrases'][self.phrase]
        self.assertIn(color,HEADING_BACKGROUNDS)
        self.assertTrue(all(style.get('backgroundColor')==color for _,style in runs))
        self.assertTrue(all(style.get('bold') for _,style in runs))
        data=self.arrange()
        node=next(n for c in data['document']['components'] for r in c.get('value') or [] for n in r['nodes'] if n['style'].get('backgroundColor'))
        node['style'].pop('backgroundColor')
        self.assertFalse(self.verify(data))

    def test_highlights_limit_three_and_reject_repeated_missing_or_heading_text(self):
        phrases=[p.split('\n')[-1] for p in self.sections]
        options=choose_visual_style(self.sections,[],phrases+['없음'])
        self.assertEqual(len(options['highlight_phrases']),3)
        self.assertEqual(len(set(options['highlight_phrases'].values())),3)
        invalid=choose_visual_style(self.sections,[],['기부단체와 세액공제를 확인해요.', self.sections[0].split('\n')[1]])
        self.assertFalse(invalid['highlight_phrases'])

    def test_overlapping_keywords_preserve_phrase_and_longest_term_color(self):
        line='기부금 세액공제와 세액공제'
        runs=line_style_runs(line,['세액공제','기부금 세액공제'],0)
        self.assertEqual(''.join(value for value,_ in runs),line)
        self.assertEqual(runs[0][0],'기부금 세액공제')
        self.assertNotEqual(runs[0][1]['fontColor'],runs[-1][1]['fontColor'])

if __name__=='__main__': unittest.main()
