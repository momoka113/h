import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as application


class ShelterRegisterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = os.path.join(self.temp_dir.name, 'shelters.json')
        self.data_file_patch = patch.object(application, 'DATA_FILE', self.data_file)
        self.data_file_patch.start()
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['username'] = 'admin'

    def tearDown(self):
        self.data_file_patch.stop()
        self.temp_dir.cleanup()

    def test_registration_persists_compatible_shelter_and_resets_form(self):
        existing = [{'id': 9, 'name': '既存施設', 'address': '既存住所'}]
        with open(self.data_file, 'w', encoding='utf-8') as data_file:
            json.dump(existing, data_file, ensure_ascii=False)

        response = self.client.post('/shelter_register', data={
            'name': '  新しい避難所  ',
            'address': '  青森市中央1-1  ',
            'capacity': '120',
            'opening_status': '開設中',
            'hazards': ['地震', '洪水'],
            'facilities': ['ペット可', 'Wi-Fi', '授乳スペース']
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('避難所を登録しました。'.encode(), response.data)
        self.assertIn('value=""'.encode(), response.data)
        with open(self.data_file, encoding='utf-8') as data_file:
            saved = json.load(data_file)

        self.assertEqual(saved[0], existing[0])
        self.assertEqual(saved[1], {
            'id': 10,
            'name': '新しい避難所',
            'address': '青森市中央1-1',
            'opening_status': '開設中',
            'capacity': 120,
            'evacuees': 0,
            'hazards': ['地震', '洪水'],
            'facilities': ['ペット可', 'Wi-Fi', '授乳スペース'],
            'pet_allowed': True,
            'barrier_free': False
        })

    def test_optional_selections_can_be_empty(self):
        response = self.client.post('/shelter_register', data={
            'name': '避難所',
            'address': '住所',
            'capacity': '1',
            'opening_status': '閉鎖中'
        })

        self.assertEqual(response.status_code, 200)
        with open(self.data_file, encoding='utf-8') as data_file:
            shelter = json.load(data_file)[0]
        self.assertEqual(shelter['hazards'], [])
        self.assertEqual(shelter['facilities'], [])

    def test_validation_rejects_bad_values_and_keeps_form_input(self):
        invalid_submissions = [
            ({'name': '  ', 'address': '住所', 'capacity': '10'},
             '避難所名を入力してください。'),
            ({'name': '避難所', 'address': '  ', 'capacity': '10'},
             '住所を入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '0'},
             '収容人数は1人以上で入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': ''},
             '収容人数を入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '1.5'},
             '収容人数は整数で入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '+10'},
             '収容人数は整数で入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': 'abc'},
             '収容人数は整数で入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '9' * 5000},
             '収容人数は整数で入力してください。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '10',
              'hazards': ['危険値']},
             '災害種別に不正な選択肢が含まれています。'),
            ({'name': '避難所', 'address': '住所', 'capacity': '10',
              'facilities': ['危険値']},
             '設備・対応に不正な選択肢が含まれています。')
        ]
        for submitted, expected_error in invalid_submissions:
            with self.subTest(expected_error=expected_error):
                response = self.client.post('/shelter_register', data=submitted)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_error.encode(), response.data)
                self.assertNotIn('避難所を登録しました。'.encode(), response.data)

        self.assertFalse(os.path.exists(self.data_file))

    def test_length_limits_are_enforced(self):
        for key, value, expected_error in (
            ('name', '避' * 201, '避難所名は200文字以内で入力してください。'),
            ('address', '住' * 301, '住所は300文字以内で入力してください。')
        ):
            submitted = {
                'name': '避難所',
                'address': '住所',
                'capacity': '5',
                key: value
            }
            response = self.client.post('/shelter_register', data=submitted)
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected_error.encode(), response.data)

    def test_unauthenticated_user_is_redirected_to_login(self):
        client = application.app.test_client()
        response = client.get('/shelter_register')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=', response.headers['Location'])

    def test_form_shows_input_examples_and_numeric_only_capacity(self):
        response = self.client.get('/shelter_register')

        self.assertEqual(response.status_code, 200)
        self.assertIn('入力例：○○小学校'.encode(), response.data)
        self.assertIn('入力例：青森市中央1丁目1-1'.encode(), response.data)
        self.assertIn('入力例：120（半角数字のみ）'.encode(), response.data)
        self.assertIn('type="text" id="capacity"'.encode(), response.data)
        self.assertIn('inputmode="numeric"'.encode(), response.data)
        self.assertIn('pattern="[0-9]+"'.encode(), response.data)

    def test_registered_hazard_is_available_in_search(self):
        self.client.post('/shelter_register', data={
            'name': '地震対応施設',
            'address': '住所',
            'capacity': '20',
            'opening_status': '閉鎖中',
            'hazards': ['土砂災害']
        })

        search_page = self.client.get('/shelter_search')
        results_page = self.client.get('/search_results?hazard=土砂災害')

        self.assertEqual(search_page.status_code, 200)
        self.assertIn('value="土砂災害"'.encode(), search_page.data)
        self.assertEqual(results_page.status_code, 200)
        self.assertIn('地震対応施設'.encode(), results_page.data)

    def test_search_filters_use_registered_hazard_and_facility_attributes(self):
        self.client.post('/shelter_register', data={
            'name': '設備対応施設',
            'address': '住所',
            'capacity': '20',
            'opening_status': '開設中',
            'hazards': ['大規模火災'],
            'facilities': ['備蓄あり', 'Wi-Fi']
        })

        search_page = self.client.get('/shelter_search')
        self.assertEqual(search_page.status_code, 200)
        for value in application.REGISTER_HAZARDS:
            self.assertIn(f'value="{value}"'.encode(), search_page.data)
        for value in application.REGISTER_FACILITIES:
            self.assertIn(f'value="{value}"'.encode(), search_page.data)
        self.assertIn(b'name="facility"', search_page.data)

        results_page = self.client.get(
            '/search_results?hazard=%E5%A4%A7%E8%A6%8F%E6%A8%A1%E7%81%AB%E7%81%BD&facility=Wi-Fi'
        )
        self.assertEqual(results_page.status_code, 200)
        self.assertIn('設備対応施設'.encode(), results_page.data)

        unmatched_results = self.client.get(
            '/search_results?facility=%E6%8E%88%E4%B9%B3%E3%82%B9%E3%83%9A%E3%83%BC%E3%82%B9'
        )
        self.assertEqual(unmatched_results.status_code, 200)
        self.assertNotIn('設備対応施設'.encode(), unmatched_results.data)

    def test_save_failure_is_reported_and_form_is_retained(self):
        with patch.object(application, 'save_shelters', side_effect=OSError('disk error')):
            response = self.client.post('/shelter_register', data={
                'name': '入力した避難所',
                'address': '青森市の住所',
                'capacity': '30',
                'opening_status': '開設中',
                'hazards': ['地震'],
                'facilities': ['Wi-Fi']
            })

        self.assertEqual(response.status_code, 500)
        self.assertIn('避難所を保存できませんでした。'.encode(), response.data)
        self.assertIn('value="入力した避難所"'.encode(), response.data)
        self.assertIn('value="地震"'.encode(), response.data)
        self.assertIn('value="Wi-Fi"'.encode(), response.data)
        self.assertIn('checked'.encode(), response.data)

    def test_registration_requires_valid_opening_status(self):
        for opening_status in ('', '未開設', '不正な値'):
            with self.subTest(opening_status=opening_status):
                response = self.client.post('/shelter_register', data={
                    'name': '避難所',
                    'address': '住所',
                    'capacity': '10',
                    'opening_status': opening_status
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn('開設状況を選択してください。'.encode(), response.data)
        self.assertFalse(os.path.exists(self.data_file))

    def test_registration_requires_review_then_confirmation_before_saving(self):
        submission = {
            'name': '確認用避難所',
            'address': '青森市の住所',
            'capacity': '25',
            'opening_status': '開設中',
            'hazards': ['津波', '洪水'],
            'facilities': ['Wi-Fi']
        }
        response = self.client.post('/shelter_register', data={
            **submission,
            'action': 'confirm'
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('登録内容の確認'.encode(), response.data)
        self.assertIn('確認用避難所'.encode(), response.data)
        self.assertIn('青森市の住所'.encode(), response.data)
        self.assertIn('津波・洪水'.encode(), response.data)
        self.assertIn('Wi-Fi'.encode(), response.data)
        self.assertFalse(os.path.exists(self.data_file))

        back_response = self.client.post('/shelter_register', data={
            **submission,
            'action': 'back'
        })
        self.assertEqual(back_response.status_code, 200)
        self.assertIn('value="確認用避難所"'.encode(), back_response.data)
        self.assertIn('value="津波"'.encode(), back_response.data)
        self.assertFalse(os.path.exists(self.data_file))

        complete_response = self.client.post('/shelter_register', data={
            **submission,
            'action': 'register'
        })
        self.assertEqual(complete_response.status_code, 200)
        self.assertIn('避難所を登録しました。'.encode(), complete_response.data)
        with open(self.data_file, encoding='utf-8') as data_file:
            saved = json.load(data_file)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['name'], '確認用避難所')

    def test_invalid_required_fields_show_error_before_confirmation(self):
        response = self.client.post('/shelter_register', data={
            'name': '  ',
            'address': '',
            'capacity': '',
            'opening_status': '閉鎖中',
            'action': 'confirm'
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('role="alert"'.encode(), response.data)
        self.assertIn('避難所名を入力してください。'.encode(), response.data)
        self.assertIn('住所を入力してください。'.encode(), response.data)
        self.assertIn('収容人数を入力してください。'.encode(), response.data)
        self.assertNotIn('登録内容の確認'.encode(), response.data)
        self.assertFalse(os.path.exists(self.data_file))

    def test_edit_updates_only_opening_status_and_preserves_other_data(self):
        original = {
            'id': 21,
            'name': '更新対象の避難所',
            'address': '青森市',
            'opening_status': '閉鎖中',
            'capacity': 50,
            'evacuees': 12,
            'hazards': ['地震'],
            'facilities': ['Wi-Fi']
        }
        with open(self.data_file, 'w', encoding='utf-8') as data_file:
            json.dump([original], data_file, ensure_ascii=False)

        edit_page = self.client.get('/shelters/21/edit')
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn('value="閉鎖中" selected'.encode(), edit_page.data)

        response = self.client.post('/shelters/21/edit', data={
            'opening_status': '開設中'
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('開設状況を更新しました。'.encode(), response.data)
        with open(self.data_file, encoding='utf-8') as data_file:
            updated = json.load(data_file)[0]
        self.assertEqual(updated['opening_status'], '開設中')
        for key in ('id', 'name', 'address', 'capacity', 'evacuees', 'hazards', 'facilities'):
            self.assertEqual(updated[key], original[key])

        register_page = self.client.get('/shelter_register')
        self.assertIn('登録済み避難所'.encode(), register_page.data)
        self.assertIn('/shelters/21/edit'.encode(), register_page.data)

    def test_edit_rejects_invalid_status_without_changing_data(self):
        original = {'id': 3, 'name': '避難所', 'opening_status': '開設中'}
        with open(self.data_file, 'w', encoding='utf-8') as data_file:
            json.dump([original], data_file, ensure_ascii=False)

        response = self.client.post('/shelters/3/edit', data={
            'opening_status': '未開設'
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('開設状況は「開設中」または「閉鎖中」を選択してください。'.encode(), response.data)
        with open(self.data_file, encoding='utf-8') as data_file:
            self.assertEqual(json.load(data_file)[0], original)

    def test_edit_requires_authentication_and_returns_not_found_for_missing_shelter(self):
        unauthenticated_client = application.app.test_client()
        response = unauthenticated_client.get('/shelters/999/edit')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=', response.headers['Location'])
        self.assertEqual(self.client.get('/shelters/999/edit').status_code, 404)

    def test_edit_save_failure_is_reported(self):
        original = {'id': 4, 'name': '避難所', 'opening_status': '閉鎖中'}
        with open(self.data_file, 'w', encoding='utf-8') as data_file:
            json.dump([original], data_file, ensure_ascii=False)

        with patch.object(application, 'save_shelters', side_effect=OSError('disk error')):
            response = self.client.post('/shelters/4/edit', data={
                'opening_status': '開設中'
            })
        self.assertEqual(response.status_code, 500)
        self.assertIn('開設状況を保存できませんでした。'.encode(), response.data)


if __name__ == '__main__':
    unittest.main()
