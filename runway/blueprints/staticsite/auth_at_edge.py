"""Blueprint for the Authorization@Edge implementation of a Static Site.

Described in detail in this blogpost:
https://aws.amazon.com/blogs/networking-and-content-delivery/authorizationedge-how-to-use-lambdaedge-and-json-web-tokens-to-enhance-web-application-security/
"""

import logging
from typing import Any, Dict, List  # pylint: disable=unused-import

import awacs.s3
from awacs.aws import Allow, Principal, Statement
from awacs.helpers.trust import make_simple_assume_policy
from troposphere import (Join, Output,  # noqa pylint: disable=unused-import
                         awslambda, cloudfront, iam, s3)

from .staticsite import StaticSite

LOGGER = logging.getLogger('runway')


class AuthAtEdge(StaticSite):
    """Auth@Edge Blueprint."""

    IAM_ARN_PREFIX = 'arn:aws:iam::aws:policy/service-role/'

    VARIABLES = {
        'AcmCertificateArn': {'type': str,
                              'default': '',
                              'description': '(Optional) Cert ARN for site'},
        'Aliases': {'type': list,
                    'default': [],
                    'description': '(Optional) Domain aliases the '
                                   'distribution'},
        'LogBucketName': {'type': str,
                          'default': '',
                          'description': 'S3 bucket for CF logs'},
        'OAuthScopes': {'type': list,
                        'default': [],
                        'description': 'OAuth2 Scopes'},
        'DisableCloudFront': {'type': bool,
                              'default': False,
                              'description': 'Whether to disable CF'},
        'PriceClass': {'type': str,
                       'default': 'PriceClass_100',  # US/Europe
                       'description': 'CF price class for the distribution.'},
        'RedirectPathSignIn': {'type': str,
                               'default': '/parseauth',
                               'description': 'Auth@Edge: The URL that should '
                                              'handle the redirect from Cognito '
                                              'after sign-in.'},
        'RedirectPathAuthRefresh': {'type': str,
                                    'default': '/refreshauth',
                                    'description': 'The URL path that should '
                                                   'handle the JWT refresh request.'},
        'RewriteDirectoryIndex': {'type': str,
                                  'default': '',
                                  'description': 'The path for rewriting the directory index'},
        'NonSPAMode': {'type': bool,
                       'default': False,
                       'description': 'Whether Auth@Edge should omit SPA specific settings'},
        'SignOutUrl': {'type': str,
                       'default': '/signout',
                       'description': 'The URL path that you can visit to sign-out.'},
        'WAFWebACL': {'type': str,
                      'default': '',
                      'description': '(Optional) WAF id to associate with the '
                                     'distribution.'},
        'custom_error_responses': {'type': list,
                                   'default': [],
                                   'description': '(Optional) Custom error '
                                                  'responses.'},
    }

    def create_template(self):
        # type: () -> None
        """Create the Blueprinted template for Auth@Edge."""
        self.template.set_version('2010-09-09')
        self.template.set_description(
            'Authorization@Edge Static Website - Bucket, Lambdas, and Distribution'
        )

        # Resources
        bucket = self.add_bucket()
        # self.add_bucket_policy(bucket)
        oai = self.add_origin_access_identity()
        bucket_policy = self.add_cloudfront_bucket_policy(bucket, oai)
        # @TODO: Make this available in Auth@Edge
        lambda_function_associations = []

        if self.directory_index_specified:
            rewrite_role = self.add_index_rewrite_role()
            index_rewrite = self.add_cloudfront_directory_index_rewrite(rewrite_role)
            index_rewrite_version = self.add_cloudfront_directory_index_rewrite_version(
                index_rewrite
            )
            lambda_function_associations = self.get_directory_index_lambda_association(
                lambda_function_associations,
                index_rewrite_version
            )

        # Auth@Edge Lambdas
        lambda_execution_role = self.add_lambda_execution_role()
        check_auth_lambda = self.get_auth_at_edge_lambda_and_ver(
            'CheckAuth',
            'Check Authorization information for request',
            'check_auth',
            lambda_execution_role
        )
        http_headers_lambda = self.get_auth_at_edge_lambda_and_ver(
            'HttpHeaders',
            'Additional Headers added to every response',
            'http_headers',
            lambda_execution_role
        )
        parse_auth_lambda = self.get_auth_at_edge_lambda_and_ver(
            'ParseAuth',
            'Parse the Authorization Headers/Cookies for the request',
            'parse_auth',
            lambda_execution_role
        )
        refresh_auth_lambda = self.get_auth_at_edge_lambda_and_ver(
            'RefreshAuth',
            'Refresh the Authorization information when expired',
            'refresh_auth',
            lambda_execution_role
        )
        sign_out_lambda = self.get_auth_at_edge_lambda_and_ver(
            'SignOut',
            'Sign the User out of the application',
            'sign_out',
            lambda_execution_role
        )

        # CloudFront Distribution
        distribution_options = self.get_distribution_options(
            bucket,
            oai,
            lambda_function_associations,
            check_auth_lambda['version'],
            http_headers_lambda['version'],
            parse_auth_lambda['version'],
            refresh_auth_lambda['version'],
            sign_out_lambda['version']
        )
        self.add_cloudfront_distribution(
            bucket_policy,
            distribution_options
        )

    def get_auth_at_edge_lambda_and_ver(self,
                                        title,  # type: str
                                        description,  # type: str
                                        handle,  # type: str
                                        role  # iam.Role
                                       ):  # noqa: E124
        # type: (...) -> Dict[str, Any]
        """Create a lambda function and its version.

        Args:
            title (str): The name of the function in PascalCase
            description (str): Description to be displayed in the
                lambda panel
            handle (str): The underscore separated representation
                of the name of the lambda. This handle is used to
                determine the handler for the lambda as well as
                identify the correct Code hook_data information.
            role (IAM.Role): The Lambda Execution Role
        """
        function = self.get_auth_at_edge_lambda(
            title,
            description,
            handle,
            role
        )

        return {
            'function': function,
            'version': self.add_version(title, function)
        }

    def get_auth_at_edge_lambda(self,
                                title,   # type: str
                                description,   # type: str
                                handler,  # type: str
                                role  # type: iam.Role
                               ):  # noqa: E124
        # type: (...) -> awslambda.Function
        """Create an Auth@Edge lambda resource.

        Args:
            name (str): The name of the function in PascalCase
            description (str): Description to be displayed in the
                lambda panel
            handle (str): The underscore separated representation
                of the name of the lambda. This handle is used to
                determine the handler for the lambda as well as
                identify the correct Code hook_data information.
            role (IAM.Role): The Lambda Execution Role
        """
        return self.template.add_resource(
            awslambda.Function(
                title,
                Code=self.context.hook_data['aae_lambda_config'][handler],
                Description=description,
                Handler='__init__.handler',
                Role=role.get_att("Arn"),
                Runtime='python3.7'
            )
        )

    def add_version(self,
                    title,  # type: str
                    lambda_function  # type: awslambda.Function
                   ):  # noqa E214
        # type: (...) -> awslambda.Version
        """Create a version association with a Lambda@Edge function.

        In order to ensure different versions of the function
        are appropriately uploaded a hash based on the code of the
        lambda is appended to the name. As the code changes so
        will this hash value.

        Args:
            title (str): The name of the function in PascalCase
            role (awslambda.Function): The Lambda function
        """
        s3_key = lambda_function.properties['Code'].to_dict()['S3Key']
        code_hash = s3_key.split('.')[0].split('-')[-1]
        return self.template.add_resource(
            awslambda.Version(
                title + 'Ver' + code_hash,
                FunctionName=lambda_function.ref()
            )
        )

    def add_lambda_execution_role(self):
        # type: () -> iam.Role
        """Create the Lambda@Edge execution role."""
        return self.template.add_resource(
            iam.Role(
                'LambdaExecutionRole',
                AssumeRolePolicyDocument=make_simple_assume_policy(
                    'lambda.amazonaws.com', 'edgelambda.amazonaws.com'
                ),
                ManagedPolicyArns=[
                    self.IAM_ARN_PREFIX + 'AWSLambdaBasicExecutionRole'
                ]
            )
        )

    def get_distribution_options(self,
                                 bucket,  # type: s3.Bucket
                                 oai,  # type: cloudfront.CloudFrontOriginAccessIdentity
                                 lambda_funcs,  # type: List[cloudfront.LambdaFunctionAssociation]
                                 check_auth_lambda_version,  # type: awslambda.Version
                                 http_headers_lambda_version,  # type: awslambda.Version
                                 parse_auth_lambda_version,  # type: awslambda.Version
                                 refresh_auth_lambda_version,  # type: awslambda.Version
                                 sign_out_lambda_version  # type: awslambda.Version
                                ):  # noqa: E124
        # type: (...) -> Dict[str, Any]
        """Retrieve the options for our CloudFront distribution.

        Keyword Args:
            bucket (dict): The bucket resource
            oai (dict): The origin access identity resource

        Return:
            dict: The CloudFront Distribution Options

        """
        variables = self.get_variables()

        default_cache_behavior_lambdas = lambda_funcs
        default_cache_behavior_lambdas.append(
            cloudfront.LambdaFunctionAssociation(
                EventType='viewer-request',
                LambdaFunctionARN=check_auth_lambda_version.ref()
            )
        )
        default_cache_behavior_lambdas.append(
            cloudfront.LambdaFunctionAssociation(
                EventType='origin-response',
                LambdaFunctionARN=http_headers_lambda_version.ref()
            )
        )

        return {
            'Aliases': self.add_aliases(),
            'Origins': [
                cloudfront.Origin(
                    DomainName=Join(
                        '.',
                        [bucket.ref(),
                         's3.amazonaws.com']),
                    S3OriginConfig=cloudfront.S3OriginConfig(
                        OriginAccessIdentity=Join(
                            '',
                            ['origin-access-identity/cloudfront/',
                             oai.ref()])
                    ),
                    Id='protected-bucket'
                )
            ],
            'CacheBehaviors': [
                cloudfront.CacheBehavior(
                    PathPattern=variables['RedirectPathSignIn'],
                    Compress=True,
                    ForwardedValues=cloudfront.ForwardedValues(
                        QueryString=True
                    ),
                    LambdaFunctionAssociations=[
                        cloudfront.LambdaFunctionAssociation(
                            EventType='viewer-request',
                            LambdaFunctionARN=parse_auth_lambda_version.ref()
                        )
                    ],
                    TargetOriginId='protected-bucket',
                    ViewerProtocolPolicy="redirect-to-https"
                ),
                cloudfront.CacheBehavior(
                    PathPattern=variables['RedirectPathAuthRefresh'],
                    Compress=True,
                    ForwardedValues=cloudfront.ForwardedValues(
                        QueryString=True
                    ),
                    LambdaFunctionAssociations=[
                        cloudfront.LambdaFunctionAssociation(
                            EventType='viewer-request',
                            LambdaFunctionARN=refresh_auth_lambda_version.ref()
                        )
                    ],
                    TargetOriginId='protected-bucket',
                    ViewerProtocolPolicy="redirect-to-https"
                ),
                cloudfront.CacheBehavior(
                    PathPattern=variables['SignOutUrl'],
                    Compress=True,
                    ForwardedValues=cloudfront.ForwardedValues(
                        QueryString=True
                    ),
                    LambdaFunctionAssociations=[
                        cloudfront.LambdaFunctionAssociation(
                            EventType='viewer-request',
                            LambdaFunctionARN=sign_out_lambda_version.ref()
                        )
                    ],
                    TargetOriginId='protected-bucket',
                    ViewerProtocolPolicy="redirect-to-https"
                ),
            ],
            'DefaultCacheBehavior': cloudfront.DefaultCacheBehavior(
                AllowedMethods=['GET', 'HEAD'],
                Compress=True,
                DefaultTTL='86400',
                ForwardedValues=cloudfront.ForwardedValues(
                    QueryString=True,
                ),
                LambdaFunctionAssociations=default_cache_behavior_lambdas,
                TargetOriginId='protected-bucket',
                ViewerProtocolPolicy='redirect-to-https'
            ),
            'DefaultRootObject': 'index.html',
            'Logging': self.add_logging_bucket(),
            'PriceClass': variables['PriceClass'],
            'Enabled': True,
            'WebACLId': self.add_web_acl(),
            'CustomErrorResponses': self._get_error_responses(),
            'ViewerCertificate': self.add_acm_cert()
        }

    def _get_error_responses(self):
        """Return error response based on site stack variables.

        When custom_error_responses are defined return those, if running
        in NonSPAMode return nothing, or return the standard error responses
        for a SPA.
        """
        variables = self.get_variables()
        if variables['custom_error_responses']:
            return [
                cloudfront.CustomErrorResponse(
                    ErrorCode=response['ErrorCode'],
                    ResponseCode=response['ResponseCode'],
                    ResponsePagePath=response['ResponsePagePath']
                ) for response in variables['custom_error_responses']
            ]
        if variables['NonSPAMode']:
            return []
        return [
            cloudfront.CustomErrorResponse(
                ErrorCode=404,
                ResponseCode=200,
                ResponsePagePath='/index.html'
            )
        ]

    def _get_cloudfront_bucket_policy_statements(self, bucket, oai):
        return [
            Statement(
                Action=[awacs.s3.GetObject],
                Effect=Allow,
                Principal=Principal(
                    'CanonicalUser',
                    oai.get_att('S3CanonicalUserId')
                ),
                Resource=[
                    Join('', [bucket.get_att('Arn'), '/*'])
                ]
            ),
            Statement(
                Action=[awacs.s3.ListBucket],
                Effect=Allow,
                Principal=Principal(
                    'CanonicalUser',
                    oai.get_att('S3CanonicalUserId')
                ),
                Resource=[bucket.get_att('Arn')]
            )
        ]
