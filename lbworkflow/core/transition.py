from django.utils import timezone

from lbworkflow.models import Event, WorkItem

from .sendmsg import wf_send_msg


def create_event(instance, transition, **kwargs):
    act_type = 'transition' if transition.pk else transition.code
    event = Event.objects.create(
        instance=instance, transition=transition, act_type=act_type,
        **kwargs)
    return event


class TransitionExecutor(object):
    def __init__(
            self, operator, instance, workitem, transition=None,
            comment='', attachments=[]):
        self.wf_obj = instance.content_object
        self.instance = instance
        self.operator = operator
        self.workitem = workitem
        self.transition = transition

        self.comment = comment
        self.attachments = attachments

        self.from_activity = instance.cur_activity
        # hold&assign wouldn't change activity
        self.to_activity = transition.output_activity
        self.all_todo_workitems = instance.get_todo_workitems()

        self.last_event = None

    def execute(self):
        # TODO check permission

        all_todo_workitems = self.all_todo_workitems
        need_transfer = False
        if self.transition.routing_rule == 'joint' and self.transition.code not in ['back to', 'rollback']:
            if all_todo_workitems.count() == 1:
                need_transfer = True
        else:
            need_transfer = True
        self._complete_workitem(need_transfer)
        if not need_transfer:
            return

        self._do_transfer()

        # if is agree should check if need auto agree for next activity
        if self.transition.is_agree:
            self._auto_agree_next_activity()

    def _auto_agree_next_activity(self):
        instance = self.instance

        agree_transition = instance.get_agree_transition()
        all_todo_workitems = instance.get_todo_workitems()

        if not agree_transition:
            return

        for workitem in all_todo_workitems:
            users = [workitem.user, workitem.agent_user]
            users = [e for e in users]
            for user in set(users):
                if self.instance.cur_activity != workitem.activity:  # has processed
                    return
                if instance.is_user_agreed(user):
                    TransitionExecutor(self.operator, instance, workitem, agree_transition).execute()

    def _complete_workitem(self, need_transfer):
        """ close workite, create event and return it """
        instance = self.instance
        workitem = self.workitem
        transition = self.transition

        workitem.status = 'completed'
        workitem.save()

        to_activity = self.to_activity if need_transfer else workitem.activity

        event = create_event(
            instance, transition,
            comment=self.comment, user=self.operator,
            old_activity=workitem.activity, new_activity=to_activity,
            workitem=workitem)

        event.attachments.add(*self.attachments)

        self.last_event = event

        return event

    def _do_transfer_for_instance(self):
        instance = self.instance
        wf_obj = self.wf_obj

        from_activity = self.from_activity
        from_status = from_activity.status

        to_activity = self.to_activity
        to_status = self.to_activity.status

        # Submit
        if not from_activity.is_submitted() and to_activity.is_submitted():
            instance.submit_time = timezone.now()
            wf_obj.on_submit()

        # cancel & give up & reject
        if from_activity.is_submitted() and not to_activity.is_submitted():
            wf_obj.on_fail()

        # complete
        if from_status != 'completed' and to_status == 'completed':
            instance.end_on = timezone.now()
            self.wf_obj.on_complete()

        # cancel complete
        if from_status == 'completed' and to_status != 'completed':
            instance.end_on = None

        instance.cur_activity = self.to_activity
        self.wf_obj.on_do_transition(from_activity, to_activity)

        instance.save()

    def _send_notification(self):
        instance = self.instance
        last_event = self.last_event

        notice_users = last_event.notice_users.exclude(
            pk__in=[self.operator.pk, instance.created_by.pk]).distinct()
        wf_send_msg(notice_users, 'notify', last_event)

        # send notification to instance.created_by
        if instance.created_by != self.operator:
            wf_send_msg([instance.created_by], 'transfered', last_event)

    def _gen_new_workitem(self):
        last_event = self.last_event

        if not last_event:
            return

        next_operators = last_event.next_operators.distinct()

        need_notify_operators = []
        for operator in next_operators:
            new_workitem = WorkItem(
                instance=self.instance, activity=self.to_activity,
                user=operator)
            new_workitem.update_authorization(commit=True)

            # notify next operator(not include current operator and instance.created_by)
            if operator not in [self.operator, self.instance.created_by]:
                need_notify_operators.append(operator)

            agent_user = new_workitem.agent_user
            if agent_user and agent_user not in [self.operator, self.instance.created_by]:
                need_notify_operators.append(agent_user)

        wf_send_msg(need_notify_operators, 'new_workitem', last_event)

    def update_users_on_transfer(self):
        instance = self.instance
        event = self.last_event
        to_activity = event.new_activity

        next_operators = to_activity.get_operators(instance.created_by, self.operator, instance)
        event.next_operators.add(*next_operators)
        notice_users = to_activity.get_notice_users(instance.created_by, self.operator, instance)
        event.notice_users.add(*notice_users)
        can_view_users = to_activity.get_share_users(instance.created_by, self.operator, instance)
        instance.can_view_users.add(*can_view_users)

    def _do_transfer(self):
        self.update_users_on_transfer()
        # auto complete all current work item
        self.all_todo_workitems.update(status='completed')
        self._do_transfer_for_instance()
        self._gen_new_workitem()
        self._send_notification()