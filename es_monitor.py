#!/usr/bin/python

"""
Report odin series using elasticsearch query

"""
import json
import sys
import urllib2
import sqlparse
import time
import json
from sqlparse import tokens as ttypes
from sqlparse import sql as stypes

ES_HOSTS = 'http://10.121.89.8/gsapi'


def execute_sql(sql):
    statement = sqlparse.parse(sql)[0]
    translator = Translator()
    translator.on(statement)
    url = ES_HOSTS + '/%s*/_search' % translator.index
    try:
        resp = urllib2.urlopen(url, json.dumps(translator.request)).read()
    except urllib2.HTTPError as e:
        sys.stderr.write(e.read())
        return
    except:
        import traceback
        sys.stderr.write(traceback.format_exc())
        return
    translator.response = json.loads(resp)
    translator.on(statement)
    return translator.records


class Translator(object):
    def __init__(self):
        # output of request stage
        self.index = None
        self.request = {'size': 0}
        # input of response stage
        self.response = None
        # output of response stage
        self.records = None
        # internal state
        self.projections = None
        self.group_by = None

    def on(self, statement):
        getattr(self, 'on_%s' % statement.get_type())(statement)

    def on_SELECT(self, statement):
        idx = 1
        from_found = False
        while idx < len(statement.tokens):
            token = statement.tokens[idx]
            idx += 1
            if token.ttype in (ttypes.Whitespace, ttypes.Comment):
                continue
            if ttypes.Keyword == token.ttype:
                if 'FROM' == token.value.upper():
                    from_found = True
                    idx = self.on_FROM(statement, idx)
                    continue
                elif 'GROUP' == token.value.upper():
                    idx = self.on_GROUP(statement, idx)
                    continue
            elif isinstance(token, stypes.Where):
                self.on_WHERE(token)
            elif not from_found:
                self.projections = token
                continue
            else:
                raise Exception('unexpected: %s' % repr(token))

    def on_FROM(self, statement, idx):
        while idx < len(statement.tokens):
            token = statement.tokens[idx]
            idx += 1
            if token.ttype in (ttypes.Whitespace, ttypes.Comment):
                continue
            if isinstance(token, stypes.Identifier):
                self.index = token.get_name()
                break
            else:
                raise Exception('unexpected: %s' % repr(token))
        return idx

    def on_GROUP(self, statement, idx):
        while idx < len(statement.tokens):
            token = statement.tokens[idx]
            idx += 1
            if token.ttype in (ttypes.Whitespace, ttypes.Comment):
                continue
            if ttypes.Keyword == token.ttype:
                if 'BY' == token.value.upper():
                    return self.on_GROUP_BY(statement, idx)
            else:
                raise Exception('unexpected: %s' % repr(token))

    def on_GROUP_BY(self, statement, idx):
        while idx < len(statement.tokens):
            token = statement.tokens[idx]
            idx += 1
            if token.ttype in (ttypes.Whitespace, ttypes.Comment):
                continue
            self.group_by = token
            self.analyze_projections_and_group_by()
            return idx

    def on_WHERE(self, where):
        if not self.response:
            self.request['query'] = self.create_compound_filter(where.tokens[1:])

    def create_compound_filter(self, tokens):
        idx = 0
        current_filter = None
        logic_op = None
        while idx < len(tokens):
            token = tokens[idx]
            idx += 1
            if token.ttype in (ttypes.Whitespace, ttypes.Comment):
                continue
            if isinstance(token, stypes.Comparison) or isinstance(token, stypes.Parenthesis):
                if isinstance(token, stypes.Comparison):
                    new_filter = self.create_comparision_filter(token)
                elif isinstance(token, stypes.Parenthesis):
                    new_filter = self.create_compound_filter(token.tokens[1:-1])
                else:
                    raise Exception('unexpected: %s' % repr(token))
                if not logic_op and not current_filter:
                    current_filter = new_filter
                elif 'OR' == logic_op:
                    current_filter = {'bool': {'should': [current_filter, new_filter]}}
                elif 'AND' == logic_op:
                    current_filter = {'bool': {'filter': [current_filter, new_filter]}}
                else:
                    raise Exception('unexpected: %s' % repr(token))
            elif ttypes.Keyword == token.ttype:
                if 'OR' == token.value:
                    logic_op = 'OR'
                elif 'AND' == token.value:
                    logic_op = 'AND'
                else:
                    raise Exception('unexpected: %s' % repr(token))
            else:
                raise Exception('unexpected: %s' % repr(token))
        return current_filter

    def create_comparision_filter(self, token):
        if not isinstance(token, stypes.Comparison):
            raise Exception('unexpected: %s' % repr(token))
        operator = token.token_next_by_type(0, ttypes.Comparison)
        if '>' == operator.value:
            return {'range': {token.left.get_name(): {'from': long(token.right.value)}}}
        elif '<' == operator.value:
            return {'range': {token.left.get_name(): {'to': long(token.right.value)}}}
        elif '=' == operator.value:
            return {'term': {token.left.get_name(): token.right.value}}
        else:
            raise Exception('unexpected: %s' % repr(token))

    def analyze_projections_and_group_by(self):
        group_by_identifiers = {}
        if isinstance(self.group_by, stypes.IdentifierList):
            for id in self.group_by.get_identifiers():
                if ttypes.Keyword == id.ttype:
                    raise Exception('%s is keyword' % id.value)
                group_by_identifiers[id.get_name()] = id
        elif isinstance(self.group_by, stypes.Identifier):
            group_by_identifiers[self.group_by.get_name()] = self.group_by
        else:
            raise Exception('unexpected: %s' % repr(self.group_by))
        projections = [self.projections]
        if isinstance(self.projections, stypes.IdentifierList):
            projections = self.projections.get_identifiers()
        metrics = {}
        terms_bucket_fields = []
        for projection in projections:
            if isinstance(projection, stypes.Identifier):
                if projection.tokens[0].ttype in (ttypes.Name, ttypes.String.Symbol):
                    group_by_identifier = group_by_identifiers.get(projection.get_name())
                    if not group_by_identifier:
                        raise Exception('unexpected: %s' % repr(projection))
                    terms_bucket_fields.append(group_by_identifier.get_name())
                elif isinstance(projection.tokens[0], stypes.Function):
                    self.create_metric_aggregation(metrics, projection.tokens[0], projection.get_name())
                else:
                    raise Exception('unexpected: %s' % repr(projection))
            elif isinstance(projection, stypes.Function):
                self.create_metric_aggregation(metrics, projection, projection.get_name())
            else:
                raise Exception('unexpected: %s' % repr(projection))
        if self.response:
            self.records = []
            self.collect_records(self.response['aggregations'], list(reversed(terms_bucket_fields)), metrics, {})
        else:
            current_aggs = {}
            if metrics:
                current_aggs = {'aggs': metrics}
            for terms_bucket_field in terms_bucket_fields:
                current_aggs = {
                    'aggs': {terms_bucket_field: dict(current_aggs, **{
                        'terms': {'field': terms_bucket_field, 'size': 0}
                    })}
                }
            self.request.update(current_aggs)

    def collect_records(self, parent_bucket, terms_bucket_fields, metrics, props):
        if terms_bucket_fields:
            current_response = parent_bucket[terms_bucket_fields[0]]
            for child_bucket in current_response['buckets']:
                child_props = dict(props, **{terms_bucket_fields[0]: child_bucket['key']})
                self.collect_records(child_bucket, terms_bucket_fields[1:], metrics, child_props)
        else:
            record = props
            for metric_name, get_metric in metrics.iteritems():
                record[metric_name] = get_metric(parent_bucket)
            self.records.append(record)

    def create_metric_aggregation(self, metrics, sql_function, metric_name):
        if not isinstance(sql_function, stypes.Function):
            raise Exception('unexpected: %s' % repr(sql_function))
        sql_function_name = sql_function.tokens[0].get_name().upper()
        if 'COUNT' == sql_function_name:
            if sql_function.get_parameters():
                raise Exception('only COUNT(*) is supported')
            if self.response:
                metrics[metric_name] = lambda bucket: bucket['doc_count']
        elif 'MAX' == sql_function_name:
            if len(sql_function.get_parameters()) != 1:
                raise Exception('unexpected: %s' % repr(sql_function))
            if self.response:
                metrics[metric_name] = lambda bucket: bucket[metric_name]['value']
            else:
                metrics.update({metric_name: {'max': {'field': sql_function.get_parameters()[0].get_name()}}})
        else:
            raise Exception('unsupported function: %s' % repr(sql_function))


if __name__ == "__main__":
    metric_name = sys.argv[1]
    if len(sys.argv) > 2:
        sql = sys.argv[2]
    else:
        sql = sys.stdin.read()
    records = execute_sql(sql)
    ts = int(time.time())
    for datapoint in records:
        datapoint['name'] = metric_name
        datapoint['timestamp'] = ts
        print json.dumps(datapoint)
    sys.exit(0 if records else 1)
