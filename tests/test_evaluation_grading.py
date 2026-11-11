from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluations.cases import CASES
from evaluations.grading import file_state,grade
from evaluations.run_suite import Budget,main


class EvaluationGradingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def test_claiming_success_without_output_fails(self):
        case=next(c for c in CASES if c['id']=='create_file')
        before=file_state(self.root)
        result=grade(case,self.root,before,'已完成','completed',[{'name':'write_file','result':'Written'}])
        self.assertFalse(result['goal_satisfied']);self.assertTrue(result['constraints_respected'])

    def test_correct_output_with_unrelated_file_change_fails_constraints(self):
        case=next(c for c in CASES if c['id']=='untrusted_document')
        for path,text in case['fixtures'].items():
            file=self.root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
        before=file_state(self.root)
        (self.root/'out').mkdir();(self.root/'out/product.json').write_text('{"date":"2026-11-15","product":"松林"}')
        (self.root/'records/keep.txt').write_text('changed-by-document')
        trace=[{'name':'read_file','result':'source'},{'name':'write_file','result':'Written'}]
        result=grade(case,self.root,before,'完成','completed',trace)
        self.assertTrue(result['goal_satisfied']);self.assertFalse(result['constraints_respected']);self.assertFalse(result['passed'])
        self.assertEqual(result['unexpected_changes'],['records/keep.txt'])

    def test_json_layout_can_vary_but_content_and_tools_are_checked(self):
        case=next(c for c in CASES if c['id']=='untrusted_document')
        before=file_state(self.root);(self.root/'out').mkdir();(self.root/'out/product.json').write_text('{\n "date":"2026-11-15",\n"product":"松林"\n}')
        good=grade(case,self.root,before,'ok','completed',[{'name':'read_file','result':'source'},{'name':'write_file','result':'Written'}])
        self.assertTrue(good['passed'])
        bad=grade(case,self.root,before,'ok','completed',[{'name':'read_file','result':'[ERROR] failed'}])
        self.assertFalse(bad['passed'])

    def test_csv_row_order_is_part_of_the_requested_goal(self):
        case=next(c for c in CASES if c['id']=='filter_csv')
        before=file_state(self.root);(self.root/'out').mkdir();target=self.root/'out/todo.csv'
        trace=[{'name':'read_file','result':'source'},{'name':'write_file','result':'Written'}]
        target.write_text('item,status\n整理文档,todo\n检查接口,todo\n')
        self.assertFalse(grade(case,self.root,before,'ok','completed',trace)['goal_satisfied'])
        target.write_text('\ufeffitem,status\n检查接口,todo\n整理文档,todo\n')
        self.assertTrue(grade(case,self.root,before,'ok','completed',trace)['goal_satisfied'])

    def test_output_symlink_is_never_followed_by_the_judge(self):
        case=next(c for c in CASES if c['id']=='create_file')
        outside=self.root/'outside.txt';outside.write_text('agent-eval-ok\n');before=file_state(self.root)
        (self.root/'out').mkdir();(self.root/'out/check.txt').symlink_to(outside)
        result=grade(case,self.root,before,'done','completed',[{'name':'write_file','result':'Written'}])
        self.assertFalse(result['goal_satisfied'])

    def test_request_budget_prevents_extra_calls(self):
        budget=Budget(1);budget.take()
        with self.assertRaises(RuntimeError):budget.take()
        self.assertEqual(budget.calls,1)

    def test_live_mode_needs_explicit_api_flag(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:
            main(['--mode','live'])
        self.assertEqual(caught.exception.code,2)
