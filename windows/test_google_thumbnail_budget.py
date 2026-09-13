import io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from naver_automation import NaverAutomation
from blog_google_budget import GoogleImageChallengeError
import test_naver_publish as support

class ThumbnailBudgetTests(unittest.TestCase):
    def test_only_visual_selected_two_thumbnails_open_and_one_query(self):
        with tempfile.TemporaryDirectory() as folder:
            helper = support.ReferenceCaptureLoadingTests()
            app, driver, state = helper.setup_capture(folder, loading=False, photo_count=12)
            data=io.BytesIO();Image.new('RGB',(200,150),'green').save(data,'PNG')
            for i in range(12):
                driver.find_elements('id',f'dimg_{i}')[0].screenshot_as_png=data.getvalue()
            observed=[]
            def select(items):
                observed.extend(items)
                return [8,2,8,3,999]
            with patch('naver_automation.WebDriverWait',helper.PollingWait):
                result=app.capture_google_reference_candidates('photo',Path(folder),thumbnail_selector=select)
            self.assertEqual(len(observed),8)
            self.assertTrue(all(Path(item['path']).is_file() for item in observed))
            self.assertEqual([item['search_rank'] for item in result],[8,2])
            self.assertEqual(state['clicks'],2)
            driver.get.assert_called_once()
            self.assertIn('udm=2',driver.get.call_args.args[0])

    def test_rejected_thumbnail_batch_never_opens_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            helper=support.ReferenceCaptureLoadingTests()
            app,driver,state=helper.setup_capture(folder,loading=False,photo_count=4)
            data=io.BytesIO();Image.new('RGB',(200,150),'green').save(data,'PNG')
            for i in range(4): driver.find_elements('id',f'dimg_{i}')[0].screenshot_as_png=data.getvalue()
            with patch('naver_automation.WebDriverWait',helper.PollingWait):
                result=app.capture_google_reference_candidates('photo',Path(folder),thumbnail_selector=lambda _: [])
            self.assertEqual(result,[])
            self.assertEqual(state['clicks'],0)
            app._inspect_reference_license.assert_not_called()

    def test_captcha_stops_before_preview_without_alternate_search(self):
        with tempfile.TemporaryDirectory() as folder:
            helper=support.ReferenceCaptureLoadingTests()
            app,driver,state=helper.setup_capture(folder,loading=False)
            driver.current_url='https://www.google.com/sorry/index'
            with patch('naver_automation.WebDriverWait',helper.PollingWait):
                with self.assertRaises(GoogleImageChallengeError):
                    app.capture_google_reference_candidates('photo',Path(folder))
            self.assertEqual(state['clicks'],0)
            driver.get.assert_called_once()
            record=json.loads((Path(folder)/'google_reference_diagnostics.json').read_text(encoding='utf-8'))
            self.assertEqual(record['status'],'challenge')

if __name__=='__main__':unittest.main()
