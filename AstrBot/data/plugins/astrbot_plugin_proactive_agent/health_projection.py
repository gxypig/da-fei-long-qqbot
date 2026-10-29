"""Connection, necessary delivery and queue health are separate observations."""
def runtime_health(plugin):
    delivery=plugin._delivery.health();queue=plugin._reply_queue
    waiting=[queue.jobs[k] for k in queue.pending if k in queue.jobs]
    delivery['reply_backlog']=len(waiting)
    delivery['oldest_reply_wait_seconds']=max((queue.clock()-j.received for j in waiting),default=0)
    delivery['suspended_replies']=len(getattr(queue,'suspended',{}))
    delivery['backlog_delayed']=delivery['oldest_reply_wait_seconds']>60
    delivery['ordinary_delivery_ok']=not delivery.get('unresolved_necessary')
    backups=getattr(plugin,'_backups',None)
    delivery['backup']={'enabled':plugin._cfg('hourly_data_backup_enabled',True),
        **(dict(backups.state) if backups else {'status':'not_started'})}
    return delivery
