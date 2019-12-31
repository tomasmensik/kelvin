import json
import os
import re
import django_rq
from datetime import datetime

from django.shortcuts import render, redirect
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.utils import timezone as tz

from ..task_utils import highlight_code, render_markdown

from common.models import Submit, Class, AssignedTask, Task
from common.evaluate import evaluate_job
from api.models import UserToken
from kelvin.settings import BASE_DIR, MAX_INLINE_CONTENT_BYTES
from ..forms import UploadSolutionForm
from evaluator.testsets import TestSet
from common.evaluate import get_meta
from evaluator.results import EvaluationResult
from .utils import is_teacher

@login_required()
def student_index(request):
    result = []

    now = datetime.now()
    classess = Class.objects.current_semester().filter(students__pk=request.user.id)

    for clazz in classess:
        tasks = []
        for assignment in AssignedTask.objects.filter(clazz_id=clazz.id).order_by('-id'):
            last_submit = Submit.objects.filter(
                assignment__id=assignment.id,
                student__id=request.user.id,
            ).last()

            data = {
                'id': assignment.id,
                'name': assignment.task.name,
                'points': None,
                'max_points': None,
                'deadline': assignment.deadline,
                'tznow': tz.now(),
            }

            if last_submit:
                data['points'] = last_submit.points
                data['max_points'] = last_submit.max_points

            tasks.append(data)

        result.append({
            'class': clazz,
            'tasks': tasks,
        })

    return render(request, 'web/index.html', {
        'classess': result,
        'token': UserToken.objects.get(user__id=request.user.id).token,
    })


def get(submit):
    results = []
    try:
        path = re.sub(r'^submits/', 'submit_results/', str(submit.source))
        path = path.rstrip('.c')
        results = EvaluationResult(path)
    except json.JSONDecodeError as e:
        # TODO: show error
        pass

    data = {
        "submit": submit,
        "results": results,
        "source": highlight_code(submit.source.path),
    }
    return data


@login_required()
def task_detail(request, assignment_id, submit_num=None, student_username=None):
    submits = Submit.objects.filter(
        assignment__pk=assignment_id,
    ).order_by('-id')

    if is_teacher(request.user):
        submits = submits.filter(student__username=student_username)
    else:
        submits = submits.filter(student__pk=request.user.id)

    assignment = AssignedTask.objects.get(id=assignment_id)

    task_dir = os.path.join(BASE_DIR, "tasks", assignment.task.code)

    data = {
        # TODO: task and deadline can be combined into assignment ad deal with it in template
        'task': assignment.task,
        'deadline': assignment.deadline,
        'submits': submits,
        'text': render_markdown(task_dir, assignment.task.code),
        'inputs': TestSet(task_dir, get_meta(request.user)),
        'tznow': tz.now(),
        'max_inline_content_bytes': MAX_INLINE_CONTENT_BYTES,
    }

    current_submit = None
    if submit_num:
        current_submit = submits.get(submit_num=submit_num)
    elif submits:
        current_submit = submits[0]

    if current_submit:
        data = {**data, **get(current_submit)}

    if request.method == 'POST':
        form = UploadSolutionForm(request.POST, request.FILES)
        if form.is_valid():
            s = Submit()
            s.source = request.FILES['solution']
            s.student = request.user
            s.assignment = assignment
            s.submit_num = Submit.objects.filter(assignment__id=s.assignment.id,
                                                 student__id=request.user.id).count() + 1
            s.save()
            django_rq.enqueue(evaluate_job, s)
            return redirect(request.path_info + '#result')
    else:
        form = UploadSolutionForm()
    data['upload_form'] = form
    return render(request, 'web/task_detail.html', data)

def raw_test_content(request, task_name, test_name, file):
    task = Task.objects.get(code=task_name)

    task_dir = os.path.join(BASE_DIR, "tasks", task.code)
    tests = TestSet(task_dir, get_meta(request.user))

    for test in tests:
        if test.name == test_name:
            if file in test.files:
                return HttpResponse(test.files[file].read(), 'text/plain')
    return HttpResponseNotFound()

@login_required
def raw_result_content(request, submit_id, test_name, result_type, file):
    submit = Submit.objects.get(pk=submit_id)
    
    if submit.student_id != request.user.id and not is_teacher(request.user):
        return HttpResponseForbidden()

    for pipe in get(submit)['results']:
        for test in pipe.tests:
            if test.name == test_name:
                if file in test.files:
                    if result_type in test.files[file]:
                        return HttpResponse(test.files[file][result_type].read(), 'text/html' if result_type == 'html' else 'text/plain')
    return HttpResponseNotFound()

@login_required
def submit_download(request, assignment_id, login, submit_num):
    submit = Submit.objects.get(
            assignment_id=assignment_id,
            student__username=login,
            submit_num=submit_num
    )

    if not is_teacher(request.user) and login != submit.student.username:
        return HttpResponseForbidden()

    res = HttpResponse(submit.source, 'text/plain')
    res['Content-Disposition'] = f'attachment; filename="{login}_{submit_num}.c"'
    return res

def script(request, token):
    data = {
        "token": token,
    }
    return render(request, "web/install.sh", data, "text/x-shellscript")


def uprpy(request):
    with open(os.path.join(BASE_DIR, "scripts/submit.py")) as f:
        return HttpResponse(f.read(), 'text/x-python')


@login_required
def project(request, project_type):
    if project_type not in ['normal', 'bonus']:
        return HttpResponse(code=404)

    with open(os.path.join(BASE_DIR, "projects", project_type, "assigned", f"{request.user.username}.html")) as f:
        return HttpResponse(f.read(), 'text/html')
