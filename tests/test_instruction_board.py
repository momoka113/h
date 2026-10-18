import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as application


class InstructionBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.shelter_file = os.path.join(self.temp_dir.name, 'shelters.json')
        self.instruction_file = os.path.join(self.temp_dir.name, 'instructions.json')
        self.shelter_file_patch = patch.object(application, 'DATA_FILE', self.shelter_file)
        self.instruction_file_patch = patch.object(
            application, 'INSTRUCTIONS_FILE', self.instruction_file
        )
        self.shelter_file_patch.start()
        self.instruction_file_patch.start()
        with open(self.shelter_file, 'w', encoding='utf-8') as data_file:
            json.dump([
                {'id': 17, 'name': '第一避難所'},
                {'id': 42, 'name': '第二避難所'}
            ], data_file, ensure_ascii=False)
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['username'] = 'admin'

    def tearDown(self):
        self.instruction_file_patch.stop()
        self.shelter_file_patch.stop()
        self.temp_dir.cleanup()

    def post_instruction(self, **overrides):
        values = {
            'target': '住民',
            'department': '',
            'urgency': '高',
            'district': 'A地区',
            'shelter_id': '42',
            'content': '  避難してください  '
        }
        values.update(overrides)
        return self.client.post('/board', data=values)

    def read_instructions(self):
        with open(self.instruction_file, encoding='utf-8') as data_file:
            return json.load(data_file)

    def test_public_board_shows_resident_list_but_protects_registration_and_status_updates(self):
        client = application.app.test_client()
        with open(self.instruction_file, 'w', encoding='utf-8') as data_file:
            json.dump([
                {'id': 1, 'target': '住民', 'content': '住民に公開する指示', 'status': '未対応'},
                {'id': 2, 'target': '職員', 'content': '職員だけの指示', 'status': '未対応'},
                {'id': 3, 'target': '各部署', 'department': '福祉部署',
                 'content': '部署だけの指示', 'status': '未対応'}
            ], data_file, ensure_ascii=False)

        board = client.get('/board')
        self.assertEqual(board.status_code, 200)
        self.assertIn('住民に公開する指示'.encode(), board.data)
        self.assertNotIn('職員だけの指示'.encode(), board.data)
        self.assertNotIn('部署だけの指示'.encode(), board.data)
        self.assertNotIn('指示を登録'.encode(), board.data)
        self.assertNotIn(b'name="status"', board.data)

        post_response = client.post('/board', data={'content': '指示'})
        self.assertEqual(post_response.status_code, 302)
        self.assertIn('/login?next=', post_response.headers['Location'])

        response = client.post('/board/1/status', data={'status': '完了'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=', response.headers['Location'])

        with self.client.session_transaction() as session:
            session['logged_in'] = True
        logged_in_board = self.client.get('/board')
        self.assertIn('指示を登録'.encode(), logged_in_board.data)
        self.assertIn('職員だけの指示'.encode(), logged_in_board.data)
        self.assertIn('部署だけの指示'.encode(), logged_in_board.data)
        self.assertIn(b'name="status"', logged_in_board.data)

    def test_valid_instruction_is_persisted_and_post_redirects(self):
        response = self.post_instruction()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/board')
        saved = self.read_instructions()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['id'], 1)
        self.assertEqual(saved[0]['target'], '住民')
        self.assertEqual(saved[0]['department'], '')
        self.assertEqual(saved[0]['urgency'], '高')
        self.assertEqual(saved[0]['content'], '避難してください')
        self.assertEqual(saved[0]['district'], 'A地区')
        self.assertEqual(saved[0]['shelter_id'], 42)
        self.assertEqual(saved[0]['shelter'], '第二避難所')
        self.assertEqual(saved[0]['status'], '未対応')
        self.assertTrue(saved[0]['created_at'])
        self.assertEqual(saved[0]['created_at'], saved[0]['updated_at'])

        board = self.client.get(response.headers['Location'])
        self.assertEqual(board.status_code, 200)
        self.assertIn('指示を登録しました。'.encode(), board.data)
        self.assertIn('避難してください'.encode(), board.data)
        self.assertNotIn('指示を登録しました。'.encode(), self.client.get('/board').data)

    def test_shelter_is_optional_but_a_supplied_shelter_id_must_exist(self):
        response = self.post_instruction(shelter_id='')

        self.assertEqual(response.status_code, 302)
        saved = self.read_instructions()[0]
        self.assertEqual(saved['shelter_id'], '')
        self.assertEqual(saved['shelter'], '')

        board = self.client.get('/board')
        self.assertEqual(board.status_code, 200)
        self.assertIn('避難先: 指定なし'.encode(), board.data)

        invalid_response = self.post_instruction(shelter_id='9999')
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn('登録済みの避難先を選択してください。'.encode(), invalid_response.data)
        self.assertEqual(len(self.read_instructions()), 1)

    def test_department_is_required_only_for_department_recipient(self):
        missing_department = self.post_instruction(target='各部署')
        self.assertEqual(missing_department.status_code, 400)
        self.assertIn('部署を選択してください。'.encode(), missing_department.data)
        self.assertFalse(os.path.exists(self.instruction_file))

        response = self.post_instruction(
            target='各部署', department='福祉部署', district='B地区'
        )
        self.assertEqual(response.status_code, 302)
        saved = self.read_instructions()[0]
        self.assertEqual(saved['target'], '各部署')
        self.assertEqual(saved['department'], '福祉部署')
        self.assertEqual(saved['district'], 'B地区')

        unexpected_department = self.post_instruction(department='福祉部署')
        self.assertEqual(unexpected_department.status_code, 400)
        self.assertIn('部署を指定できません'.encode(), unexpected_department.data)
        self.assertEqual(len(self.read_instructions()), 1)

    def test_invalid_fields_and_nonexistent_shelter_are_rejected(self):
        invalid_cases = (
            ({'content': ' \t '}, '指示内容を入力してください。'),
            ({'target': '管理者'}, '指示の宛先を選択してください。'),
            ({'urgency': '緊急'}, '緊急度を選択してください。'),
            ({'target': '各部署', 'department': '存在しない部署'}, '部署を選択してください。'),
            ({'district': 'C地区'}, '対象地区を選択してください。'),
            ({'shelter_id': '0'}, '登録済みの避難先を選択してください。'),
        )
        for overrides, expected_error in invalid_cases:
            with self.subTest(overrides=overrides):
                response = self.post_instruction(**overrides)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_error.encode(), response.data)
                self.assertNotIn('指示を登録しました。'.encode(), response.data)
        self.assertFalse(os.path.exists(self.instruction_file))

    def test_department_field_is_conditionally_shown_and_cleared(self):
        response = self.client.get('/board')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="instructionDepartmentField" hidden', response.data)
        self.assertIn(b'id="instructionDepartment" name="department" disabled', response.data)
        self.assertIn(b"department.value = ''", response.data)

    def test_existing_shelter_district_values_are_reused(self):
        with open(self.shelter_file, 'w', encoding='utf-8') as data_file:
            json.dump([
                {'id': 17, 'name': '第一避難所', 'address': '青森市中央1-1'}
            ], data_file, ensure_ascii=False)

        board = self.client.get('/board')
        self.assertIn('value="中央"'.encode(), board.data)
        self.assertNotIn('value="A地区"'.encode(), board.data)

        response = self.post_instruction(shelter_id='17', district='中央')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.read_instructions()[0]['district'], '中央')

    def test_list_is_newest_first_and_handles_legacy_or_malformed_fields(self):
        with open(self.instruction_file, 'w', encoding='utf-8') as data_file:
            json.dump([
                {
                    'id': 1, 'target': '住民', 'content': '古い指示',
                    'district': 'A地区', 'shelter_id': 17,
                    'created_at': '2026年10月17日 09:00', 'status': '未対応'
                },
                {
                    'id': 2, 'target': '住民',
                    'content': '<script>alert(1)</script>', 'district': 'B地区',
                    'shelter_id': 42, 'created_at': '2026年10月18日 09:00',
                    'status': '対応中'
                },
                {'id': 3, 'target': '住民', 'content': {}, 'shelter': None},
                {'target': '住民', 'content': '記録IDなし'},
                {'target': '職員', 'content': '内部の指示'},
                {'target': '各部署', 'department': '福祉部署', 'content': '部署専用の指示'}
            ], data_file, ensure_ascii=False)

        response = self.client.get('/board')
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertLess(body.index('&lt;script&gt;'), body.index('古い指示'))
        self.assertIn('住民向け', body)
        self.assertIn('対象地区: B地区', body)
        self.assertIn('避難先: 第二避難所', body)
        self.assertIn('2026年10月18日 09:00', body)
        self.assertIn('2026年10月17日 09:00', body)
        self.assertIn('不明', body)
        self.assertIn('記録IDなし', body)
        self.assertNotIn('<script>alert(1)</script>', body)
        self.assertIn('職員向け指示一覧', body)
        self.assertIn('内部の指示', body)
        self.assertIn('部署専用の指示', body)
        self.assertIn('各部署向け（福祉部署）', body)

    def test_urgency_sort_orders_high_medium_low_and_high_is_red(self):
        with open(self.instruction_file, 'w', encoding='utf-8') as data_file:
            json.dump([
                {
                    'id': 1, 'target': '住民', 'urgency': '低',
                    'content': '低の指示', 'created_at': '2026年10月18日 09:03'
                },
                {
                    'id': 2, 'target': '住民', 'urgency': '中',
                    'content': '中の指示', 'created_at': '2026年10月18日 09:02'
                },
                {
                    'id': 3, 'target': '住民',
                    'urgency': '高', 'content': '高の指示',
                    'created_at': '2026年10月18日 09:01'
                }
            ], data_file, ensure_ascii=False)

        response = self.client.get('/board?sort=urgency')
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        self.assertLess(body.index('高の指示'), body.index('中の指示'))
        self.assertLess(body.index('中の指示'), body.index('低の指示'))
        self.assertIn('class="instruction-high-urgency"', body)
        self.assertIn('class="urgency-high">高</span>', body)
        self.assertIn('登録日時順に戻す', body)

        default_order = self.client.get('/board').data.decode()
        self.assertLess(default_order.index('低の指示'), default_order.index('中の指示'))
        self.assertIn('緊急度順に並び替え', default_order)

    def test_status_controls_update_and_persist_allowed_values(self):
        self.post_instruction()
        saved = self.read_instructions()[0]
        for status in ('対応中', '完了', '未対応'):
            board = self.client.get('/board')
            self.assertEqual(board.status_code, 200)
            self.assertIn(f'name="status"'.encode(), board.data)
            self.assertIn('対応状況を更新'.encode(), board.data)
            response = self.client.post(
                f"/board/{saved['id']}/status",
                data={'status': status}
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.read_instructions()[0]['status'], status)
            self.assertIn(
                '対応状況を更新しました。'.encode(),
                self.client.get('/board').data
            )

    def test_status_update_rejects_invalid_value_and_unknown_instruction(self):
        self.post_instruction()
        original = self.read_instructions()[0].copy()
        invalid_status = self.client.post(
            f"/board/{original['id']}/status",
            data={'status': '解除'}
        )
        self.assertEqual(invalid_status.status_code, 302)
        self.assertEqual(self.read_instructions()[0], original)
        self.assertIn(
            '対応状況は'.encode(),
            self.client.get('/board').data
        )

        missing_instruction = self.client.post(
            '/board/999/status',
            data={'status': '完了'}
        )
        self.assertEqual(missing_instruction.status_code, 302)
        self.assertEqual(self.read_instructions()[0], original)
        self.assertIn(
            '更新する指示が見つかりませんでした。'.encode(),
            self.client.get('/board').data
        )

    def test_status_save_failure_is_reported_without_changing_saved_status(self):
        self.post_instruction()
        original = self.read_instructions()[0].copy()
        with patch.object(
            application, 'save_instructions', side_effect=OSError('disk error')
        ):
            response = self.client.post(
                f"/board/{original['id']}/status",
                data={'status': '対応中'}
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.read_instructions()[0], original)
        self.assertIn(
            '対応状況を保存できませんでした。'.encode(),
            self.client.get('/board').data
        )

    def test_empty_state_and_save_error_are_reported(self):
        empty_board = self.client.get('/board')
        self.assertIn('登録されている指示はありません。'.encode(), empty_board.data)

        with patch.object(
            application, 'save_instructions', side_effect=OSError('disk error')
        ):
            response = self.post_instruction()
        self.assertEqual(response.status_code, 500)
        self.assertIn('指示を保存できませんでした。'.encode(), response.data)
        self.assertIn('避難してください'.encode(), response.data)
        self.assertNotIn('指示を登録しました。'.encode(), response.data)
        self.assertFalse(os.path.exists(self.instruction_file))

    def test_public_homepage_only_shows_resident_instructions(self):
        records = [
            {'target': '住民', 'content': '住民向けのお知らせ', 'status': '未対応'},
            {'target': '各部署', 'department': '福祉部署', 'content': '内部指示', 'status': '未対応'}
        ]
        with open(self.instruction_file, 'w', encoding='utf-8') as data_file:
            json.dump(records, data_file, ensure_ascii=False)
        response = application.app.test_client().get('/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('住民向けのお知らせ'.encode(), response.data)
        self.assertNotIn('内部指示'.encode(), response.data)
        self.assertIn('避難指示'.encode(), response.data)
        self.assertNotIn('新着指示'.encode(), response.data)
        self.assertIn('class="search-icon"'.encode(), response.data)
        self.assertIn('text-decoration: underline'.encode(), response.data)
        self.assertIn('setView([40.8246, 140.7400], 12)'.encode(), response.data)

    def test_concurrent_registrations_receive_unique_ids(self):
        def submit(index):
            client = application.app.test_client()
            with client.session_transaction() as session:
                session['logged_in'] = True
                session['username'] = 'admin'
            return client.post('/board', data={
                'target': '職員',
                'department': '',
                'urgency': '中',
                'district': 'A地区',
                'shelter_id': '17',
                'content': f'指示 {index}'
            }).status_code

        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(submit, range(4)))

        self.assertEqual(statuses, [302] * 4)
        saved = self.read_instructions()
        self.assertEqual(len(saved), 4)
        self.assertEqual({item['id'] for item in saved}, {1, 2, 3, 4})
